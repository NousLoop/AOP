"""Enhanced concurrency control (P36.5).

This module implements per-node and per-skill concurrency limits,
resource-aware scheduling, and backpressure mechanisms.
"""

from __future__ import annotations

from defaults import database_url as resolve_database_url

import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from db import connect

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

@dataclass
class ConcurrencyLimit:
    """Concurrency limit configuration."""
    per_node: int = 1
    per_skill: int = 10
    per_task: int = 5
    global_limit: int = 100

@dataclass
class ConcurrencyState:
    """Current concurrency state."""
    active_nodes: int = 0
    active_by_skill: dict[str, int] = None
    active_by_task: dict[str, int] = None
    active_global: int = 0
    
    def __post_init__(self):
        if self.active_by_skill is None:
            self.active_by_skill = {}
        if self.active_by_task is None:
            self.active_by_task = {}

class ConcurrencyController:
    """Enhanced concurrency controller with resource awareness."""
    
    def __init__(
        self,
        database_url: str | None = None,
        limits: ConcurrencyLimit | None = None,
    ):
        self.database_url = resolve_database_url(database_url)
        self.limits = limits or ConcurrencyLimit()
        self.state = ConcurrencyState()
        self.last_refresh = 0.0
        self.refresh_interval = 5.0  # Refresh state every 5 seconds
    
    def refresh_state(self) -> None:
        """Refresh concurrency state from database."""
        now = time.time()
        if now - self.last_refresh < self.refresh_interval:
            return
        
        with connect(self.database_url) as conn:
            # Count active nodes
            active_nodes = conn.execute(
                """
                SELECT COUNT(*) as count
                FROM task_nodes
                WHERE status = 'running'
                """,
            ).fetchone()
            self.state.active_nodes = active_nodes["count"] if active_nodes else 0
            
            # Count by skill
            by_skill = conn.execute(
                """
                SELECT agent_key, COUNT(*) as count
                FROM task_nodes
                WHERE status = 'running'
                GROUP BY agent_key
                """,
            ).fetchall()
            self.state.active_by_skill = {row["agent_key"]: row["count"] for row in by_skill}
            
            # Count by task
            by_task = conn.execute(
                """
                SELECT task_id::text, COUNT(*) as count
                FROM task_nodes
                WHERE status = 'running'
                GROUP BY task_id
                """,
            ).fetchall()
            self.state.active_by_task = {row["task_id"]: row["count"] for row in by_task}
            
            self.state.active_global = self.state.active_nodes
            self.last_refresh = now
    
    def can_execute(
        self,
        task_id: str,
        node_key: str,
        skill: str,
    ) -> tuple[bool, str]:
        """Check if execution is allowed given current concurrency limits."""
        self.refresh_state()
        
        # Check global limit
        if self.state.active_global >= self.limits.global_limit:
            return False, f"Global concurrency limit reached ({self.limits.global_limit})"
        
        # Check per-task limit
        task_active = self.state.active_by_task.get(task_id, 0)
        if task_active >= self.limits.per_task:
            return False, f"Task concurrency limit reached ({self.limits.per_task})"
        
        # Check per-skill limit
        skill_active = self.state.active_by_skill.get(skill, 0)
        if skill_active >= self.limits.per_skill:
            return False, f"Skill concurrency limit reached ({self.limits.per_skill})"
        
        # Check per-node limit (check if same node is already running)
        with connect(self.database_url) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) as count
                FROM task_nodes
                WHERE node_key = %s AND status = 'running'
                """,
                (node_key,),
            ).fetchone()
            node_active = row["count"] if row else 0
        
        if node_active >= self.limits.per_node:
            return False, f"Node concurrency limit reached ({self.limits.per_node})"
        
        return True, "OK"
    
    def acquire_execution_slot(
        self,
        task_id: str,
        node_key: str,
        skill: str,
    ) -> bool:
        """Acquire an execution slot."""
        can_execute, reason = self.can_execute(task_id, node_key, skill)
        if not can_execute:
            return False
        
        # Increment counters in memory (will be refreshed from DB)
        self.state.active_global += 1
        self.state.active_by_task[task_id] = self.state.active_by_task.get(task_id, 0) + 1
        self.state.active_by_skill[skill] = self.state.active_by_skill.get(skill, 0) + 1
        
        return True
    
    def release_execution_slot(
        self,
        task_id: str,
        node_key: str,
        skill: str,
    ) -> None:
        """Release an execution slot."""
        self.state.active_global = max(0, self.state.active_global - 1)
        self.state.active_by_task[task_id] = max(0, self.state.active_by_task.get(task_id, 0) - 1)
        self.state.active_by_skill[skill] = max(0, self.state.active_by_skill.get(skill, 0) - 1)
    
    def get_backpressure(self) -> float:
        """Calculate backpressure signal (0.0 = no pressure, 1.0 = full pressure)."""
        self.refresh_state()
        
        # Calculate backpressure based on global limit
        global_ratio = self.state.active_global / self.limits.global_limit
        
        # Also consider skill limits
        skill_ratios = [
            count / self.limits.per_skill
            for count in self.state.active_by_skill.values()
        ]
        if skill_ratios:
            max_skill_ratio = max(skill_ratios)
        else:
            max_skill_ratio = 0.0
        
        # Return the higher of the two ratios
        return max(global_ratio, max_skill_ratio)
    
    def get_concurrency_stats(self) -> dict[str, Any]:
        """Get current concurrency statistics."""
        self.refresh_state()
        
        return {
            "active_nodes": self.state.active_nodes,
            "active_global": self.state.active_global,
            "active_by_skill": self.state.active_by_skill,
            "active_by_task": self.state.active_by_task,
            "limits": {
                "per_node": self.limits.per_node,
                "per_skill": self.limits.per_skill,
                "per_task": self.limits.per_task,
                "global": self.limits.global_limit,
            },
            "backpressure": self.get_backpressure(),
        }

# Singleton instance
_concurrency_controller: ConcurrencyController | None = None

def get_concurrency_controller() -> ConcurrencyController:
    """Get or create the singleton concurrency controller."""
    global _concurrency_controller
    if _concurrency_controller is None:
        _concurrency_controller = ConcurrencyController()
    return _concurrency_controller
