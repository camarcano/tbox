/**
 * Scouting Report Application
 *
 * Generates Inside Edge-style scouting reports with:
 *   - Zone charts (vs Fastballs / vs Other Pitches)
 *   - Pitch type performance table
 *   - By-count breakdown table
 *   - Full PDF export (VS ALL / VS RHP / VS LHP)
 */

class ScoutingApp {
    constructor() {
        this.initElements();
        this.attachEventListeners();
        this.checkDbStatus();
        this.searchTimeout = null;
    }

    initElements() {
        this.playerSearch = document.getElementById("playerSearch");
        this.searchResults = document.getElementById("searchResults");
        this.selectedPlayerId = document.getElementById("selectedPlayerId");
        this.selectedPlayerName = document.getElementById("selectedPlayerName");
        this.seasonInput = document.getElementById("season");
        this.vsHandSelect = document.getElementById("vsHand");
        this.startDateInput = document.getElementById("startDate");
        this.endDateInput = document.getElementById("endDate");
        this.generateBtn = document.getElementById("generateBtn");
        this.exportPdfBtn = document.getElementById("exportPdfBtn");
        this.dbStatus = document.getElementById("dbStatus");

        this.loadingSection = document.getElementById("loadingSection");
        this.reportSection = document.getElementById("reportSection");
        this.reportHeader = document.getElementById("reportHeader");
        this.errorSection = document.getElementById("errorSection");
        this.errorMessage = document.getElementById("errorMessage");
        this.retryBtn = document.getElementById("retryBtn");
    }

    attachEventListeners() {
        this.playerSearch.addEventListener("input", () => this.handleSearchInput());
        this.playerSearch.addEventListener("focus", () => {
            if (this.searchResults.children.length > 0) this.searchResults.style.display = "block";
        });
        document.addEventListener("click", (e) => {
            if (!e.target.closest(".player-search-field")) this.searchResults.style.display = "none";
        });
        this.generateBtn.addEventListener("click", () => this.handleGenerate());
        this.exportPdfBtn.addEventListener("click", () => this.handleExportPdf());
        this.retryBtn.addEventListener("click", () => this.hideError());
        this.seasonInput.addEventListener("change", () => this.checkDbStatus());
    }

    // -- Player search --------------------------------------------------------

    handleSearchInput() {
        const q = this.playerSearch.value.trim();
        if (q.length < 2) {
            this.searchResults.style.display = "none";
            return;
        }
        clearTimeout(this.searchTimeout);
        this.searchTimeout = setTimeout(() => this.fetchSearchResults(q), 250);
    }

    async fetchSearchResults(q) {
        const season = this.seasonInput.value;
        try {
            const resp = await fetch(`/api/scouting/search?q=${encodeURIComponent(q)}&season=${season}`);
            const data = await resp.json();
            this.renderSearchResults(data.results || []);
        } catch (e) {
            console.error("Search error:", e);
        }
    }

    renderSearchResults(results) {
        this.searchResults.innerHTML = "";
        if (results.length === 0) {
            this.searchResults.innerHTML = '<li class="suggestion-empty">No players found</li>';
            this.searchResults.style.display = "block";
            return;
        }
        for (const r of results) {
            const li = document.createElement("li");
            li.className = "suggestion-item";
            li.textContent = r.name;
            li.addEventListener("click", () => this.selectPlayer(r));
            this.searchResults.appendChild(li);
        }
        this.searchResults.style.display = "block";
    }

    selectPlayer(player) {
        this.selectedPlayerId.value = player.mlbamid;
        this.playerSearch.value = player.name;
        this.selectedPlayerName.textContent = `MLBAMID: ${player.mlbamid}`;
        this.searchResults.style.display = "none";
    }

    // -- DB status ------------------------------------------------------------

    async checkDbStatus() {
        try {
            const season = this.seasonInput.value;
            const resp = await fetch(`/api/statcast/status?season=${season}`);
            const data = await resp.json();
            if (data.exists) {
                const pitches = (data.total_pitches || 0).toLocaleString();
                this.dbStatus.textContent = `DB ready: ${pitches} pitches`;
                this.dbStatus.className = "db-ready";
            } else {
                this.dbStatus.textContent = "No DB found (build one from Hitters or Pitchers page)";
                this.dbStatus.className = "db-missing";
            }
        } catch (e) {
            this.dbStatus.textContent = "Could not check DB status";
        }
    }

    // -- Generate report ------------------------------------------------------

    async handleGenerate() {
        const batterId = this.selectedPlayerId.value;
        if (!batterId) {
            this.showError("Please search and select a player first.");
            return;
        }

        this.generateBtn.disabled = true;
        this.hideError();
        this.reportSection.style.display = "none";
        this.loadingSection.style.display = "block";

        try {
            const resp = await fetch("/api/scouting/report", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    season: parseInt(this.seasonInput.value),
                    batter_id: parseInt(batterId),
                    p_throws: this.vsHandSelect.value,
                    start_date: this.startDateInput.value || null,
                    end_date: this.endDateInput.value || null,
                }),
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || "Report generation failed");

            this.renderReport(data);
            this.loadingSection.style.display = "none";
            this.reportSection.style.display = "block";
        } catch (err) {
            this.loadingSection.style.display = "none";
            this.showError(err.message);
        } finally {
            this.generateBtn.disabled = false;
        }
    }

    // -- Render report --------------------------------------------------------

    renderReport(data) {
        this.renderHeader(data.player, data.summary);
        this.renderZoneChart("zoneFB", data.zone_fb);
        this.renderZoneChart("zoneOther", data.zone_other);
        this.renderPitchTypeTable(data.pitch_type_table);
        this.renderByCountTable(data.by_count);
    }

    renderHeader(player, summary) {
        const vs = player.vs === "All" ? "" : `vs ${player.vs}HP`;
        const ba = summary.ba !== null ? summary.ba.toFixed(3) : "\u2014";
        const slg = summary.slg !== null ? summary.slg.toFixed(3) : "\u2014";
        this.reportHeader.innerHTML = `
            <div class="scout-player-name">
                <h2>${player.name} ${vs ? `<span class="scout-vs-badge">${vs}</span>` : ""}</h2>
                <span class="scout-meta">Bats: ${player.bats} | MLBAMID: ${player.mlbamid}</span>
            </div>
            <div class="scout-summary-stats">
                <div class="scout-stat"><span class="stat-val">${summary.pitches_charted.toLocaleString()}</span><span class="stat-lbl">Pitches</span></div>
                <div class="scout-stat"><span class="stat-val">${summary.pa}</span><span class="stat-lbl">PA</span></div>
                <div class="scout-stat"><span class="stat-val">${ba}</span><span class="stat-lbl">BA</span></div>
                <div class="scout-stat"><span class="stat-val">${slg}</span><span class="stat-lbl">SLG</span></div>
                <div class="scout-stat"><span class="stat-val">${summary.k_pct ?? "\u2014"}%</span><span class="stat-lbl">K%</span></div>
                <div class="scout-stat"><span class="stat-val">${summary.bb_pct ?? "\u2014"}%</span><span class="stat-lbl">BB%</span></div>
                <div class="scout-stat"><span class="stat-val">${summary.gb_pct ?? "\u2014"}%</span><span class="stat-lbl">GB%</span></div>
                <div class="scout-stat"><span class="stat-val">${summary.fb_pct ?? "\u2014"}%</span><span class="stat-lbl">FB%</span></div>
            </div>
        `;
    }

    // -- Zone chart -----------------------------------------------------------

    renderZoneChart(containerId, zoneData) {
        const el = document.getElementById(containerId);
        if (!zoneData || !zoneData.zones || Object.keys(zoneData.zones).length === 0) {
            el.innerHTML = '<p class="no-data">No data</p>';
            return;
        }

        const { zones, row_pcts, col_pcts } = zoneData;
        let html = '<div class="zone-chart-grid">';

        html += '<div class="zone-margin"></div>';
        for (let c = 0; c < 3; c++) {
            html += `<div class="zone-margin zone-col-pct">${col_pcts[c] ?? "\u2014"}%</div>`;
        }
        html += '<div class="zone-margin"></div>';

        const zoneOrder = [[1, 2, 3], [4, 5, 6], [7, 8, 9]];
        for (let r = 0; r < 3; r++) {
            html += `<div class="zone-margin zone-row-pct">${row_pcts[r] ?? "\u2014"}%</div>`;
            for (let c = 0; c < 3; c++) {
                const z = zones[String(zoneOrder[r][c])];
                const ba = z && z.ba !== null ? z.ba.toFixed(3) : "\u2014";
                const hab = z ? `${z.hits}/${z.ab}` : "0/0";
                const slg = z && z.slg !== null ? z.slg.toFixed(3) : "\u2014";
                const bgClass = this.baColorClass(z ? z.ba : null);
                const slgHigh = z && z.slg !== null && z.slg > 0.450;

                html += `<div class="zone-cell ${bgClass} ${slgHigh ? "zone-slg-high" : ""}">
                    <span class="zone-ba">${ba}</span>
                    <span class="zone-hab">${hab}</span>
                    <span class="zone-slg">${slg}</span>
                </div>`;
            }
            html += '<div class="zone-margin"></div>';
        }

        html += '</div>';
        el.innerHTML = html;
    }

    baColorClass(ba) {
        if (ba === null || ba === undefined) return "zone-neutral";
        if (ba >= 0.350) return "zone-hot";
        if (ba >= 0.300) return "zone-warm";
        if (ba >= 0.250) return "zone-mid";
        if (ba >= 0.200) return "zone-cool";
        return "zone-cold";
    }

    // -- Pitch type table -----------------------------------------------------

    renderPitchTypeTable(rows) {
        const table = document.getElementById("pitchTypeTable");
        if (!rows || rows.length === 0) {
            table.innerHTML = '<tr><td>No pitch type data</td></tr>';
            return;
        }

        const fmtBa = (s) => {
            if (!s || s.ba === null) return '<td class="ba-cell">\u2014</td>';
            const cls = this.baCellClass(s.ba);
            return `<td class="ba-cell ${cls}">${s.ba.toFixed(3)}<br><span class="hab">${s.hits}/${s.ab}</span></td>`;
        };

        let html = `<thead><tr>
            <th>Pitch Type</th>
            <th>All Counts<br><span class="th-sub">BA (H/AB)</span></th>
            <th>First Pitch</th>
            <th>Early Counts</th>
            <th>Two Strikes</th>
            <th>Hitter Ahead</th>
            <th>Hitter Behind</th>
            <th>With RISP</th>
            <th>Chase%</th>
            <th>Take%<br><span class="th-sub">(in zone)</span></th>
        </tr></thead><tbody>`;

        for (const r of rows) {
            html += `<tr>
                <td class="pt-name">${r.pitch_type}</td>
                ${fmtBa(r.all_counts)}
                ${fmtBa(r.first_pitch)}
                ${fmtBa(r.early_counts)}
                ${fmtBa(r.two_strikes)}
                ${fmtBa(r.hitter_ahead)}
                ${fmtBa(r.hitter_behind)}
                ${fmtBa(r.with_risp)}
                <td>${r.chase_pct !== null ? r.chase_pct + "%" : "\u2014"}</td>
                <td>${r.take_pct !== null ? r.take_pct + "%" : "\u2014"}</td>
            </tr>`;
        }

        html += '</tbody>';
        table.innerHTML = html;
    }

    baCellClass(ba) {
        if (ba === null) return "";
        if (ba >= 0.350) return "ba-hot";
        if (ba >= 0.300) return "ba-warm";
        if (ba >= 0.250) return "ba-mid";
        if (ba >= 0.200) return "ba-cool";
        return "ba-cold";
    }

    // -- By count table -------------------------------------------------------

    renderByCountTable(data) {
        const table = document.getElementById("byCountTable");
        if (!data) {
            table.innerHTML = '<tr><td>No count data</td></tr>';
            return;
        }

        const countKeys = [
            "0-0", "0-1", "0-2",
            "1-0", "1-1", "1-2",
            "2-0", "2-1", "2-2",
            "3-0", "3-1", "3-2",
        ];

        const fmtVal = (v) => (v !== null && v !== undefined) ? v : "\u2014";
        const fmtBa = (v) => (v !== null && v !== undefined) ? v.toFixed(3) : "\u2014";
        const fmtPct = (v) => (v !== null && v !== undefined) ? v + "%" : "\u2014";

        let html = '<thead><tr><th></th>';
        for (const k of countKeys) html += `<th>${k}</th>`;
        html += '<th>All</th></tr></thead><tbody>';

        html += '<tr><td class="row-label">Swing%</td>';
        for (const k of countKeys) {
            const d = data[k];
            html += `<td>${fmtPct(d?.swing_pct)}<br><span class="hab">${d?.swing_pitches ?? ""}</span></td>`;
        }
        html += `<td>${fmtPct(data.all?.swing_pct)}<br><span class="hab">${data.all?.swing_pitches ?? ""}</span></td></tr>`;

        html += '<tr><td class="row-label">BA vs FB</td>';
        for (const k of countKeys) html += `<td>${fmtBa(data[k]?.ba_fb)}</td>`;
        html += `<td>${fmtBa(data.all?.ba_fb)}</td></tr>`;

        html += '<tr><td class="row-label">BA vs Other</td>';
        for (const k of countKeys) html += `<td>${fmtBa(data[k]?.ba_other)}</td>`;
        html += `<td>${fmtBa(data.all?.ba_other)}</td></tr>`;

        html += '<tr><td class="row-label">SLG%</td>';
        for (const k of countKeys) html += `<td>${fmtBa(data[k]?.slg)}</td>`;
        html += `<td>${fmtBa(data.all?.slg)}</td></tr>`;

        html += '<tr><td class="row-label">AB</td>';
        for (const k of countKeys) html += `<td>${fmtVal(data[k]?.ab)}</td>`;
        html += `<td>${fmtVal(data.all?.ab)}</td></tr>`;

        html += '<tr><td class="row-label">H</td>';
        for (const k of countKeys) html += `<td>${fmtVal(data[k]?.h)}</td>`;
        html += `<td>${fmtVal(data.all?.h)}</td></tr>`;

        html += '</tbody>';
        table.innerHTML = html;
    }

    // -- PDF export -----------------------------------------------------------

    async handleExportPdf() {
        const batterId = this.selectedPlayerId.value;
        if (!batterId) {
            this.showError("Please search and select a player first.");
            return;
        }

        this.exportPdfBtn.disabled = true;
        this.exportPdfBtn.textContent = "Generating PDF...";

        try {
            const resp = await fetch("/api/scouting/pdf", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    season: parseInt(this.seasonInput.value),
                    batter_id: parseInt(batterId),
                    start_date: this.startDateInput.value || null,
                    end_date: this.endDateInput.value || null,
                }),
            });

            if (!resp.ok) {
                const err = await resp.json();
                throw new Error(err.error || "PDF generation failed");
            }

            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `scouting_${this.playerSearch.value.replace(/\s+/g, "_")}.pdf`;
            a.click();
            URL.revokeObjectURL(url);
        } catch (err) {
            this.showError(err.message);
        } finally {
            this.exportPdfBtn.disabled = false;
            this.exportPdfBtn.textContent = "Export Full Report (PDF) \u2014 VS ALL / VS RHP / VS LHP";
        }
    }

    // -- UI helpers -----------------------------------------------------------

    showError(message) {
        this.errorMessage.textContent = message;
        this.errorSection.style.display = "block";
    }

    hideError() {
        this.errorSection.style.display = "none";
    }
}

document.addEventListener("DOMContentLoaded", () => {
    try {
        window.scoutApp = new ScoutingApp();
        console.log("ScoutingApp initialized");
    } catch (e) {
        console.error("Failed to initialize ScoutingApp:", e);
    }
});
