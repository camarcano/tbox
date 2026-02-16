#!/usr/bin/env python3
"""
Player ID mapping utility using SFBB Player ID Map.

Handles fuzzy matching and MLBAMID lookup for player names.
"""

import os
import unicodedata
from fuzzywuzzy import fuzz, process
import pandas as pd


class PlayerMapper:
    """Maps player names to MLBAMID using SFBB database and fuzzy matching."""

    def __init__(self, sfbb_map_path="SFBB Player ID Map - PLAYERIDMAP.csv"):
        """Initialize mapper with SFBB Player ID Map.

        Parameters
        ----------
        sfbb_map_path : str
            Path to SFBB PLAYERIDMAP.csv file
        """
        self.map_df = None
        self.name_to_id = {}
        self.load_map(sfbb_map_path)

    def load_map(self, path):
        """Load SFBB Player ID Map from CSV."""
        if not os.path.exists(path):
            print(f"Warning: SFBB Player ID Map not found at {path}")
            self.map_df = pd.DataFrame()
            return

        try:
            self.map_df = pd.read_csv(path)
            print(f"Loaded SFBB Player ID Map: {len(self.map_df)} players")

            # Determine column names (SFBB uses PLAYERNAME and MLBID)
            name_col = 'PLAYERNAME' if 'PLAYERNAME' in self.map_df.columns else 'Name'
            id_col = 'MLBID' if 'MLBID' in self.map_df.columns else 'MLBAMID'

            # Index by normalized name for quick lookup
            if name_col in self.map_df.columns and id_col in self.map_df.columns:
                for _, row in self.map_df.iterrows():
                    norm_name = self.normalize_name(row[name_col])
                    mlbid = row[id_col]
                    # Skip rows with missing MLBID
                    if norm_name and pd.notna(mlbid):
                        try:
                            self.name_to_id[norm_name] = int(mlbid)
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            print(f"Error loading SFBB map: {e}")
            self.map_df = pd.DataFrame()

    @staticmethod
    def normalize_name(name: str) -> str:
        """Normalize name for matching: 'Last, First' -> 'first last' (lowercase)."""
        if not isinstance(name, str):
            return ""
        name = name.strip()
        if "," in name:
            parts = [p.strip() for p in name.split(",", 1)]
            name = f"{parts[1]} {parts[0]}"
        name = unicodedata.normalize("NFD", name.lower())
        name = "".join(c for c in name if unicodedata.category(c) != "Mn")
        return name.replace(".", "").replace("'", "").replace("-", " ").strip()

    def lookup_exact(self, name: str) -> int:
        """Lookup MLBAMID by exact normalized match.

        Returns None if not found.
        """
        norm = self.normalize_name(name)
        return self.name_to_id.get(norm)

    def lookup_fuzzy(self, name: str, threshold: int = 60,
                     limit: int = 20):
        """Fuzzy search for players matching name.

        Parameters
        ----------
        name : str
            Player name to search
        threshold : int
            Fuzzy match threshold 0-100 (60 = 60% similarity)
        limit : int
            Maximum number of results

        Returns
        -------
        list of dict
            [{'name': '...', 'mlbamid': ..., 'score': ...}, ...]
        """
        if self.map_df.empty:
            return []

        # Determine column names
        name_col = 'PLAYERNAME' if 'PLAYERNAME' in self.map_df.columns \
            else 'Name'
        id_col = 'MLBID' if 'MLBID' in self.map_df.columns else 'MLBAMID'

        names = self.map_df[name_col].tolist()
        matches = process.extract(
            name,
            names,
            scorer=fuzz.token_set_ratio,
            limit=limit
        )

        pos_col = 'ALLPOS' if 'ALLPOS' in self.map_df.columns else None

        results = []
        for player_name, score in matches:
            if score >= threshold:
                row = self.map_df[self.map_df[name_col] == player_name].iloc[0]
                entry = {
                    'name': player_name,
                    'mlbamid': int(row[id_col]),
                    'score': score,
                }
                if pos_col and pd.notna(row.get(pos_col, None)):
                    entry['pos'] = str(row[pos_col])
                results.append(entry)
        return results

    def get_all_names(self):
        """Get list of all player names in the map."""
        if self.map_df.empty:
            return []

        name_col = 'PLAYERNAME' if 'PLAYERNAME' in self.map_df.columns \
            else 'Name'
        return self.map_df[name_col].tolist()
