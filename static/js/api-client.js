/**
 * API Client for Hitter Dashboard
 */

class APIClient {
    constructor(baseUrl = "") {
        this.baseUrl = baseUrl || "";
    }

    /**
     * Fetch dashboard data
     */
    async fetchDashboard(config) {
        const formData = new FormData();

        // Add config fields
        formData.append("season", config.season);
        formData.append("min_pa", config.minPA);
        formData.append("date_start", config.dateStart);
        formData.append("date_end", config.dateEnd);
        formData.append("skip_exit_velo", config.skipExitVelo);
        formData.append("skip_date_range", config.skipDateRange);

        // Add FG CSV file if provided
        if (config.fgCsvFile) {
            formData.append("fg_csv", config.fgCsvFile);
        }

        // Use JSON for non-file data
        const response = await fetch(`${this.baseUrl}/api/dashboard/fetch`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                season: config.season,
                min_pa: config.minPA,
                date_start: config.dateStart,
                date_end: config.dateEnd,
                skip_exit_velo: config.skipExitVelo,
                skip_date_range: config.skipDateRange,
            }),
        });

        if (!response.ok) {
            throw new Error(`API error: ${response.status}`);
        }

        return await response.json();
    }

    /**
     * Search players with fuzzy matching
     */
    async searchPlayers(query, threshold = 60, limit = 20) {
        const params = new URLSearchParams({
            q: query,
            threshold: threshold,
            limit: limit,
        });

        const response = await fetch(
            `${this.baseUrl}/api/players/search?${params}`
        );

        if (!response.ok) {
            throw new Error(`API error: ${response.status}`);
        }

        return await response.json();
    }

    /**
     * Download CSV
     */
    downloadCsv(sessionId) {
        const url = `${this.baseUrl}/api/download/csv?session_id=${sessionId}`;
        this._downloadFile(url, "hitter_dashboard.csv");
    }

    /**
     * Download Excel
     */
    downloadExcel(sessionId) {
        const url = `${this.baseUrl}/api/download/excel?session_id=${sessionId}`;
        this._downloadFile(url, "hitter_dashboard.xlsx");
    }

    /**
     * Get session data
     */
    async getSession(sessionId) {
        const response = await fetch(
            `${this.baseUrl}/api/sessions/${sessionId}`
        );

        if (!response.ok) {
            throw new Error(`API error: ${response.status}`);
        }

        return await response.json();
    }

    /**
     * Helper to download file
     */
    _downloadFile(url, filename) {
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }
}

// Export for use in app.js
const api = new APIClient();
