/**
 * Hitter Dashboard Application Logic
 *
 * Uses SSE (Server-Sent Events) for real-time progress during fetch.
 * Features: AG Grid data table, multi-player typeahead, position filter, column search.
 */

// Step definitions for mapping log messages to progress %
const STEPS = [
    { pattern: "[1/5]", pct: 10, label: "Fetching season stats + bat speed + HR..." },
    { pattern: "[2/5]", pct: 30, label: "Fetching exit-velocity buckets..." },
    { pattern: "events >= 105", pct: null, label: null },
    { pattern: "[3/5]", pct: 50, label: "Fetching date-range stats..." },
    { pattern: "[4/5]", pct: 65, label: "Loading position data..." },
    { pattern: "[5/5]", pct: 75, label: "Loading supplemental data..." },
    { pattern: "Merging", pct: 85, label: "Merging all data sources..." },
    { pattern: "Players:", pct: 95, label: "Finalizing..." },
];

class DashboardApp {
    constructor() {
        this.sessionId = null;
        this.allData = [];
        this.columns = [];
        this.progressPct = 0;
        this.logLines = [];
        this.eventSource = null;

        // AG Grid
        this.gridApi = null;

        // Multi-player select state
        this.selectedPlayerIds = new Set();
        this.selectedPlayerNames = {};  // mlbamid -> name

        this.initElements();
        this.attachEventListeners();
        this.checkDbStatus();
    }

    initElements() {
        this.seasonInput = document.getElementById("season");
        this.minPAInput = document.getElementById("minPA");
        this.dateStartInput = document.getElementById("dateStart");
        this.dateEndInput = document.getElementById("dateEnd");
        this.skipExitVeloCheckbox = document.getElementById("skipExitVelo");
        this.skipDateRangeCheckbox = document.getElementById("skipDateRange");
        this.fgCsvInput = document.getElementById("fgCsv");
        this.fetchBtn = document.getElementById("fetchBtn");

        this.progressSection = document.getElementById("progressSection");
        this.progressBar = document.getElementById("progressBar");
        this.progressStatus = document.getElementById("progressStatus");

        this.resultsSection = document.getElementById("resultsSection");
        this.playerCount = document.getElementById("playerCount");
        this.downloadCsvBtn = document.getElementById("downloadCsv");
        this.downloadExcelBtn = document.getElementById("downloadExcel");

        // Multi-player typeahead
        this.playerSearchInput = document.getElementById("playerSearchInput");
        this.playerSuggestions = document.getElementById("playerSuggestions");
        this.selectedPlayersDiv = document.getElementById("selectedPlayers");

        // Position filter
        this.positionFilter = document.getElementById("positionFilter");
        this.posMainOnly = document.getElementById("posMainOnly");

        // Column search
        this.columnFilter = document.getElementById("columnFilter");
        this.resetFiltersBtn = document.getElementById("resetFilters");

        // AG Grid container
        this.gridDiv = document.getElementById("dataGrid");

        this.tableInfo = document.getElementById("tableInfo");

        this.errorSection = document.getElementById("errorSection");
        this.errorMessage = document.getElementById("errorMessage");
        this.retryBtn = document.getElementById("retryBtn");

        this.dbStatus = document.getElementById("dbStatus");
        this.buildDbBtn = document.getElementById("buildDbBtn");
    }

    attachEventListeners() {
        this.fetchBtn.addEventListener("click", () => this.handleFetch());
        this.resetFiltersBtn.addEventListener("click", () => this.resetFilters());
        this.downloadCsvBtn.addEventListener("click", () => this.handleDownloadCsv());
        this.downloadExcelBtn.addEventListener("click", () => this.handleDownloadExcel());
        this.buildDbBtn.addEventListener("click", () => this.handleBuildDb());

        // Multi-player typeahead
        let searchTimeout = null;
        this.playerSearchInput.addEventListener("input", () => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => this.handlePlayerSearch(), 250);
        });
        this.playerSearchInput.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
                this.playerSuggestions.style.display = "none";
            }
        });
        document.addEventListener("click", (e) => {
            if (!e.target.closest(".typeahead-wrapper")) {
                this.playerSuggestions.style.display = "none";
            }
        });

        // Position filter
        this.positionFilter.addEventListener("change", () => this.applyFilters());
        this.posMainOnly.addEventListener("change", () => this.applyFilters());

        // Column search
        this.columnFilter.addEventListener("input", () => this.applyFilters());

        this.retryBtn.addEventListener("click", () => this.hideError());
    }

    // -- AG Grid setup ---------------------------------------------------

    initGrid(data, columns) {
        const columnDefs = columns.map((col, i) => {
            const def = {
                field: col,
                headerName: col,
                sortable: true,
                resizable: true,
                filter: true,
                minWidth: 80,
            };

            // Pin the Name column (usually first or second after MLBAMID)
            if (col === "Name") {
                def.pinned = "left";
                def.width = 180;
                def.minWidth = 140;
            } else if (col === "MLBAMID") {
                def.hide = true; // hide internal ID column
            } else {
                def.width = 110;
            }

            // Number formatting
            def.valueFormatter = (params) => {
                if (params.value === null || params.value === undefined) return "\u2014";
                if (typeof params.value === "number" && !Number.isInteger(params.value)) {
                    return params.value.toFixed(3);
                }
                return params.value;
            };

            return def;
        });

        const gridOptions = {
            columnDefs: columnDefs,
            rowData: data,
            defaultColDef: {
                sortable: true,
                resizable: true,
                filter: true,
                minWidth: 70,
            },
            pagination: true,
            paginationPageSize: 25,
            paginationPageSizeSelector: [10, 25, 50, 100],
            animateRows: true,
            suppressCellFocus: true,
            isExternalFilterPresent: () => this.hasExternalFilter(),
            doesExternalFilterPass: (node) => this.passesExternalFilter(node),
            onGridReady: (params) => {
                this.gridApi = params.api;
                this.updateTableInfo();
            },
            onFilterChanged: () => this.updateTableInfo(),
            onPaginationChanged: () => this.updateTableInfo(),
        };

        // Destroy previous grid if re-fetching
        if (this.gridApi) {
            this.gridApi.destroy();
            this.gridApi = null;
        }

        this.gridDiv.innerHTML = "";
        agGrid.createGrid(this.gridDiv, gridOptions);
    }

    // -- External filter for AG Grid ------------------------------------

    hasExternalFilter() {
        return this.selectedPlayerIds.size > 0
            || this.positionFilter.value !== ""
            || this.columnFilter.value.trim() !== "";
    }

    passesExternalFilter(node) {
        const row = node.data;

        // Player filter
        if (this.selectedPlayerIds.size > 0 && !this.selectedPlayerIds.has(row.MLBAMID)) {
            return false;
        }

        // Position filter
        const posValue = this.positionFilter.value;
        if (posValue) {
            const pos = row.Pos || "";
            if (!pos) return false;
            if (this.posMainOnly.checked) {
                if (pos.split("/")[0].trim() !== posValue) return false;
            } else {
                if (!pos.split("/").some(p => p.trim() === posValue)) return false;
            }
        }

        // Column search
        const searchTerm = this.columnFilter.value.toLowerCase().trim();
        if (searchTerm) {
            const match = Object.values(row).some(val =>
                val !== null && val !== undefined && String(val).toLowerCase().includes(searchTerm)
            );
            if (!match) return false;
        }

        return true;
    }

    applyFilters() {
        if (this.gridApi) {
            this.gridApi.onFilterChanged();
        }
    }

    updateTableInfo() {
        if (!this.gridApi) return;
        const displayed = this.gridApi.getDisplayedRowCount();
        const total = this.allData.length;
        this.playerCount.textContent = displayed;
        this.tableInfo.textContent = displayed < total
            ? `Showing ${displayed} of ${total} players (filtered)`
            : `Showing all ${total} players`;
    }

    // -- Fetch with SSE progress -----------------------------------------

    async handleFetch() {
        try {
            this.showProgress();
            this.hideError();
            this.hideResults();
            this.fetchBtn.disabled = true;
            this.progressPct = 0;
            this.logLines = [];

            const config = {
                season: parseInt(this.seasonInput.value),
                min_pa: parseInt(this.minPAInput.value),
                date_start: this.dateStartInput.value,
                date_end: this.dateEndInput.value,
                skip_exit_velo: this.skipExitVeloCheckbox.checked,
                skip_date_range: this.skipDateRangeCheckbox.checked,
            };

            // Use FormData if FG CSV file is selected
            let fetchOptions;
            if (this.fgCsvInput.files && this.fgCsvInput.files[0]) {
                const formData = new FormData();
                formData.append("season", config.season);
                formData.append("min_pa", config.min_pa);
                formData.append("date_start", config.date_start);
                formData.append("date_end", config.date_end);
                formData.append("skip_exit_velo", config.skip_exit_velo);
                formData.append("skip_date_range", config.skip_date_range);
                formData.append("fg_csv", this.fgCsvInput.files[0]);
                fetchOptions = { method: "POST", body: formData };
            } else {
                fetchOptions = {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(config),
                };
            }

            const resp = await fetch("/api/dashboard/fetch", fetchOptions);
            const startData = await resp.json();
            if (!resp.ok || !startData.job_id) {
                throw new Error(startData.message || "Failed to start fetch");
            }

            this.listenProgress(startData.job_id);
        } catch (err) {
            console.error("handleFetch error:", err);
            this.fetchBtn.disabled = false;
            this.showError(err.message);
            this.hideProgress();
        }
    }

    listenProgress(jobId) {
        if (this.eventSource) this.eventSource.close();

        this.eventSource = new EventSource(`/api/progress/${jobId}`);

        this.eventSource.onmessage = (event) => {
            let msg;
            try { msg = JSON.parse(event.data); }
            catch (e) { console.warn("SSE parse error:", e); return; }

            if (msg.type === "log") {
                this.handleLogMessage(msg.message);
            } else if (msg.type === "done") {
                this.eventSource.close();
                this.eventSource = null;

                this.sessionId = msg.session_id;
                this.allData = msg.data || [];
                this.columns = msg.columns || [];

                this.updateProgress("Done!", 100);
                setTimeout(() => {
                    this.hideProgress();
                    this.showResults();
                    this.initGrid(this.allData, this.columns);
                    this.fetchBtn.disabled = false;
                }, 400);
            } else if (msg.type === "error") {
                this.eventSource.close();
                this.eventSource = null;
                this.showError(msg.message);
                this.hideProgress();
                this.fetchBtn.disabled = false;
            }
        };

        this.eventSource.onerror = () => {
            this.eventSource.close();
            this.eventSource = null;
            this.showError("Connection to server lost during fetch.");
            this.hideProgress();
            this.fetchBtn.disabled = false;
        };
    }

    handleLogMessage(text) {
        this.logLines.push(text);

        for (const step of STEPS) {
            if (text.includes(step.pattern)) {
                if (step.pct !== null) {
                    this.progressPct = step.pct;
                }
                if (step.label) {
                    this.updateProgress(step.label, this.progressPct);
                    return;
                }
            }
        }

        if (this.progressPct < 95) {
            this.progressPct = Math.min(this.progressPct + 0.3, 94);
        }

        const short = text.length > 80 ? text.slice(0, 80) + "..." : text;
        this.progressStatus.textContent = short;
        this.progressBar.value = Math.round(this.progressPct);
    }

    updateProgress(message, percent) {
        this.progressStatus.textContent = message;
        this.progressBar.value = percent;
        this.progressPct = percent;
    }

    // -- Multi-player typeahead search -----------------------------------

    async handlePlayerSearch() {
        const query = this.playerSearchInput.value.trim();

        if (query.length < 2) {
            this.playerSuggestions.style.display = "none";
            return;
        }

        try {
            const resp = await fetch(
                `/api/players/search?q=${encodeURIComponent(query)}&threshold=60&limit=10`
            );
            const result = await resp.json();

            this.playerSuggestions.innerHTML = "";

            if (!result.results || result.results.length === 0) {
                this.playerSuggestions.innerHTML =
                    '<li class="suggestion-empty">No players found</li>';
                this.playerSuggestions.style.display = "block";
                return;
            }

            for (const player of result.results) {
                if (this.selectedPlayerIds.has(player.mlbamid)) continue;

                const li = document.createElement("li");
                li.className = "suggestion-item";
                const posText = player.pos ? ` (${player.pos})` : "";
                li.textContent = `${player.name}${posText}`;
                li.addEventListener("click", () => {
                    this.addPlayerChip(player.mlbamid, player.name);
                    this.playerSearchInput.value = "";
                    this.playerSuggestions.style.display = "none";
                    this.applyFilters();
                });
                this.playerSuggestions.appendChild(li);
            }

            this.playerSuggestions.style.display = "block";
        } catch (err) {
            console.error("Player search error:", err);
        }
    }

    addPlayerChip(mlbamid, name) {
        this.selectedPlayerIds.add(mlbamid);
        this.selectedPlayerNames[mlbamid] = name;
        this.renderPlayerChips();
    }

    removePlayerChip(mlbamid) {
        this.selectedPlayerIds.delete(mlbamid);
        delete this.selectedPlayerNames[mlbamid];
        this.renderPlayerChips();
        this.applyFilters();
    }

    renderPlayerChips() {
        this.selectedPlayersDiv.innerHTML = "";
        for (const id of this.selectedPlayerIds) {
            const chip = document.createElement("span");
            chip.className = "player-chip";
            chip.innerHTML = `${this.selectedPlayerNames[id]} <span class="chip-remove" data-id="${id}">&times;</span>`;
            chip.querySelector(".chip-remove").addEventListener("click", (e) => {
                this.removePlayerChip(parseInt(e.target.dataset.id));
            });
            this.selectedPlayersDiv.appendChild(chip);
        }
    }

    // -- Filters reset ---------------------------------------------------

    resetFilters() {
        this.playerSearchInput.value = "";
        this.selectedPlayerIds.clear();
        this.selectedPlayerNames = {};
        this.selectedPlayersDiv.innerHTML = "";
        this.playerSuggestions.style.display = "none";

        this.positionFilter.value = "";
        this.posMainOnly.checked = false;
        this.columnFilter.value = "";

        if (this.gridApi) {
            this.gridApi.setFilterModel(null);
            this.gridApi.onFilterChanged();
        }
    }

    // -- Downloads -------------------------------------------------------

    handleDownloadCsv() {
        if (!this.sessionId) return;
        api.downloadCsv(this.sessionId);
    }

    handleDownloadExcel() {
        if (!this.sessionId) return;
        api.downloadExcel(this.sessionId);
    }

    // -- Statcast DB -----------------------------------------------------

    async checkDbStatus() {
        try {
            const season = this.seasonInput.value;
            const resp = await fetch(`/api/statcast/status?season=${season}`);
            const data = await resp.json();
            if (data.exists) {
                const pitches = (data.total_pitches || 0).toLocaleString();
                const lastDate = data.last_date || "?";
                this.dbStatus.textContent = `DB ready: ${pitches} pitches (through ${lastDate})`;
                this.dbStatus.className = "db-ready";
                this.buildDbBtn.textContent = "Update DB";
            } else {
                this.dbStatus.textContent = "No local DB (EV buckets will use slow HTTP)";
                this.dbStatus.className = "db-missing";
                this.buildDbBtn.textContent = "Build Statcast DB";
            }
        } catch (e) {
            this.dbStatus.textContent = "Could not check DB status";
        }
    }

    async handleBuildDb() {
        this.buildDbBtn.disabled = true;
        this.buildDbBtn.textContent = "Building...";
        this.showProgress();

        try {
            const season = parseInt(this.seasonInput.value);
            const resp = await fetch("/api/statcast/build", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ season }),
            });
            const data = await resp.json();
            if (!resp.ok || !data.job_id) {
                throw new Error(data.message || "Failed to start DB build");
            }
            this.listenDbBuild(data.job_id);
        } catch (err) {
            this.buildDbBtn.disabled = false;
            this.buildDbBtn.textContent = "Build Statcast DB";
            this.showError(err.message);
            this.hideProgress();
        }
    }

    listenDbBuild(jobId) {
        if (this.eventSource) this.eventSource.close();
        this.eventSource = new EventSource(`/api/progress/${jobId}`);

        this.eventSource.onmessage = (event) => {
            let msg;
            try { msg = JSON.parse(event.data); }
            catch (e) { return; }

            if (msg.type === "log") {
                const short = msg.message.length > 80
                    ? msg.message.slice(0, 80) + "..." : msg.message;
                this.progressStatus.textContent = short;
                if (this.progressPct < 95) {
                    this.progressPct = Math.min(this.progressPct + 0.15, 94);
                    this.progressBar.value = Math.round(this.progressPct);
                }
            } else if (msg.type === "done") {
                this.eventSource.close();
                this.eventSource = null;
                this.updateProgress("DB build complete!", 100);
                setTimeout(() => {
                    this.hideProgress();
                    this.buildDbBtn.disabled = false;
                    this.checkDbStatus();
                }, 500);
            } else if (msg.type === "error") {
                this.eventSource.close();
                this.eventSource = null;
                this.showError(msg.message);
                this.hideProgress();
                this.buildDbBtn.disabled = false;
            }
        };

        this.eventSource.onerror = () => {
            this.eventSource.close();
            this.eventSource = null;
            this.showError("Connection lost during DB build.");
            this.hideProgress();
            this.buildDbBtn.disabled = false;
        };
    }

    // -- UI visibility helpers -------------------------------------------

    showProgress() {
        this.progressSection.style.display = "block";
        this.progressBar.value = 0;
        this.progressStatus.textContent = "Starting fetch...";
    }

    hideProgress() { this.progressSection.style.display = "none"; }
    showResults()  { this.resultsSection.style.display = "block"; }
    hideResults()  { this.resultsSection.style.display = "none"; }

    showError(message) {
        this.errorMessage.textContent = message;
        this.errorSection.style.display = "block";
    }

    hideError() { this.errorSection.style.display = "none"; }
}

document.addEventListener("DOMContentLoaded", () => {
    try {
        window.app = new DashboardApp();
        console.log("DashboardApp initialized successfully");
    } catch (e) {
        console.error("Failed to initialize DashboardApp:", e);
    }
});
