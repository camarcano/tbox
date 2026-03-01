/**
 * Pitcher Dashboard Application Logic
 *
 * Uses SSE (Server-Sent Events) for real-time progress during fetch.
 * Features: AG Grid data table, column text search.
 */

const STEPS = [
    { pattern: "[1/4]", pct: 15,  label: "Fetching pitcher season stats..." },
    { pattern: "[2/4]", pct: 40,  label: "Fetching Stuff+ data..." },
    { pattern: "[3/4]", pct: 60,  label: "Fetching first-half splits from DB..." },
    { pattern: "[4/4]", pct: 80,  label: "Fetching second-half splits from DB..." },
    { pattern: "Merging", pct: 90, label: "Merging all data sources..." },
    { pattern: "Players:", pct: 95, label: "Finalizing..." },
];

class PitcherApp {
    constructor() {
        this.sessionId = null;
        this.allData = [];
        this.columns = [];
        this.progressPct = 0;
        this.eventSource = null;
        this.gridApi = null;

        this.initElements();
        this.attachEventListeners();
        this.checkDbStatus();
    }

    initElements() {
        this.seasonInput  = document.getElementById("season");
        this.minBFInput   = document.getElementById("minBF");
        this.minIPInput   = document.getElementById("minIP");
        this.h1StartInput = document.getElementById("h1Start");
        this.h1EndInput   = document.getElementById("h1End");
        this.h2StartInput = document.getElementById("h2Start");
        this.h2EndInput   = document.getElementById("h2End");
        this.fetchBtn     = document.getElementById("fetchBtn");

        this.progressSection = document.getElementById("progressSection");
        this.progressBar     = document.getElementById("progressBar");
        this.progressStatus  = document.getElementById("progressStatus");

        this.resultsSection  = document.getElementById("resultsSection");
        this.playerCount     = document.getElementById("playerCount");
        this.downloadCsvBtn  = document.getElementById("downloadCsv");
        this.downloadExcelBtn = document.getElementById("downloadExcel");

        this.columnFilter   = document.getElementById("columnFilter");
        this.resetFiltersBtn = document.getElementById("resetFilters");

        this.gridDiv  = document.getElementById("dataGrid");
        this.tableInfo = document.getElementById("tableInfo");

        this.errorSection  = document.getElementById("errorSection");
        this.errorMessage  = document.getElementById("errorMessage");
        this.retryBtn      = document.getElementById("retryBtn");

        this.dbStatus   = document.getElementById("dbStatus");
        this.buildDbBtn = document.getElementById("buildDbBtn");
    }

    attachEventListeners() {
        this.fetchBtn.addEventListener("click", () => this.handleFetch());
        this.resetFiltersBtn.addEventListener("click", () => this.resetFilters());
        this.downloadCsvBtn.addEventListener("click", () => this.handleDownloadCsv());
        this.downloadExcelBtn.addEventListener("click", () => this.handleDownloadExcel());
        this.buildDbBtn.addEventListener("click", () => this.handleBuildDb());

        this.columnFilter.addEventListener("input", () => this.applyFilters());
        this.retryBtn.addEventListener("click", () => this.hideError());
    }

    // -- AG Grid setup ---------------------------------------------------

    initGrid(data, columns) {
        const columnDefs = columns.map((col) => {
            const def = {
                field: col,
                headerName: col,
                sortable: true,
                resizable: true,
                filter: true,
                minWidth: 70,
            };

            if (col === "Name") {
                def.pinned = "left";
                def.width = 180;
                def.minWidth = 140;
            } else if (col === "MLBAMID") {
                def.hide = true;
            } else {
                def.width = 100;
            }

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
            columnDefs,
            rowData: data,
            defaultColDef: { sortable: true, resizable: true, filter: true, minWidth: 70 },
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

        if (this.gridApi) {
            this.gridApi.destroy();
            this.gridApi = null;
        }
        this.gridDiv.innerHTML = "";
        agGrid.createGrid(this.gridDiv, gridOptions);
    }

    // -- External filter -------------------------------------------------

    hasExternalFilter() {
        return this.columnFilter.value.trim() !== "";
    }

    passesExternalFilter(node) {
        const row = node.data;
        const searchTerm = this.columnFilter.value.toLowerCase().trim();
        if (searchTerm) {
            return Object.values(row).some(val =>
                val !== null && val !== undefined &&
                String(val).toLowerCase().includes(searchTerm)
            );
        }
        return true;
    }

    applyFilters() {
        if (this.gridApi) this.gridApi.onFilterChanged();
    }

    updateTableInfo() {
        if (!this.gridApi) return;
        const displayed = this.gridApi.getDisplayedRowCount();
        const total = this.allData.length;
        this.playerCount.textContent = displayed;
        this.tableInfo.textContent = displayed < total
            ? `Showing ${displayed} of ${total} pitchers (filtered)`
            : `Showing all ${total} pitchers`;
    }

    // -- Fetch with SSE progress -----------------------------------------

    async handleFetch() {
        try {
            this.showProgress();
            this.hideError();
            this.hideResults();
            this.fetchBtn.disabled = true;
            this.progressPct = 0;

            const config = {
                season:   parseInt(this.seasonInput.value),
                min_bf:   parseInt(this.minBFInput.value),
                min_ip:   parseFloat(this.minIPInput.value),
                h1_start: this.h1StartInput.value,
                h1_end:   this.h1EndInput.value,
                h2_start: this.h2StartInput.value,
                h2_end:   this.h2EndInput.value,
            };

            const resp = await fetch("/api/pitcher/fetch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(config),
            });
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
                this.allData   = msg.data || [];
                this.columns   = msg.columns || [];

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
        for (const step of STEPS) {
            if (text.includes(step.pattern)) {
                if (step.pct !== null) this.progressPct = step.pct;
                if (step.label) { this.updateProgress(step.label, this.progressPct); return; }
            }
        }
        if (this.progressPct < 95) this.progressPct = Math.min(this.progressPct + 0.3, 94);
        const short = text.length > 80 ? text.slice(0, 80) + "..." : text;
        this.progressStatus.textContent = short;
        this.progressBar.value = Math.round(this.progressPct);
    }

    updateProgress(message, percent) {
        this.progressStatus.textContent = message;
        this.progressBar.value = percent;
        this.progressPct = percent;
    }

    // -- Filters reset ---------------------------------------------------

    resetFilters() {
        this.columnFilter.value = "";
        if (this.gridApi) {
            this.gridApi.setFilterModel(null);
            this.gridApi.onFilterChanged();
        }
    }

    // -- Downloads -------------------------------------------------------

    handleDownloadCsv()   { if (this.sessionId) api.downloadCsv(this.sessionId); }
    handleDownloadExcel() { if (this.sessionId) api.downloadExcel(this.sessionId); }

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
                this.dbStatus.textContent = "No local DB (splits will be unavailable)";
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
            try { msg = JSON.parse(event.data); } catch (e) { return; }

            if (msg.type === "log") {
                const short = msg.message.length > 80 ? msg.message.slice(0, 80) + "..." : msg.message;
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

    hideProgress()  { this.progressSection.style.display = "none"; }
    showResults()   { this.resultsSection.style.display = "block"; }
    hideResults()   { this.resultsSection.style.display = "none"; }

    showError(message) {
        this.errorMessage.textContent = message;
        this.errorSection.style.display = "block";
    }
    hideError() { this.errorSection.style.display = "none"; }
}

document.addEventListener("DOMContentLoaded", () => {
    try {
        window.app = new PitcherApp();
        console.log("PitcherApp initialized successfully");
    } catch (e) {
        console.error("Failed to initialize PitcherApp:", e);
    }
});
