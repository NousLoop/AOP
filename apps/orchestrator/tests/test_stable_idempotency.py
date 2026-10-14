"""Test stable idempotency key generation (P36.1 fix)."""

from __future__ import annotations

import uuid

import pytest

# Import scheduler
try:
    from scheduler import Scheduler
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False
    pytest.skip("Scheduler not available", allow_module_level=True)


@pytest.fixture
def scheduler(database_url):
    """Create a scheduler for testing."""
    if not SCHEDULER_AVAILABLE:
        pytest.skip("Scheduler not available")
    return Scheduler(database_url=database_url)


class TestStableIdempotencyKey:
    """Test stable idempotency key generation per node."""

    @pytest.fixture
    def test_task(self, scheduler):
        """Create a test task with nodes through scheduler."""
        from planner.dag import TaskPlan, PlanNode
        
        # Create a simple plan
        plan = TaskPlan(
            goal="test goal",
            title="Test Plan",
            nodes=[
                PlanNode(
                    id="node1",
                    agent_key="claude-code",
                    depends_on=[],
                ),
                PlanNode(
                    id="node2",
                    agent_key="claude-code",
                    depends_on=["node1"],
                ),
            ],
        )
        
        # Create task through scheduler
        result = scheduler.create_task(goal="test goal", plan=plan)
        task_id = result.task_id
        
        yield task_id
        
        # Cleanup
        import psycopg
        with psycopg.connect(scheduler.database_url) as conn:
            with conn.transaction():
                conn.execute("DELETE FROM task_nodes WHERE task_id = %s::uuid", (task_id,))
                conn.execute("DELETE FROM task_dependencies WHERE task_id = %s::uuid", (task_id,))
                conn.execute("DELETE FROM tasks WHERE id = %s::uuid", (task_id,))
    
    def test_stable_idempotency_key_per_node(self, scheduler, test_task):
        """Test that idempotency key is stable per node."""
        # Get idempotency key for node1
        key1_first = scheduler.get_node_idempotency_key(test_task, "node1")
        key1_second = scheduler.get_node_idempotency_key(test_task, "node1")
        
        # Should be the same
        assert key1_first is not None
        assert key1_first == key1_second
        
        # Different nodes should have different keys
        key2 = scheduler.get_node_idempotency_key(test_task, "node2")
        assert key2 is not None
        assert key1_first != key2
    
    def test_idempotency_key_format(self, scheduler, test_task):
        """Test that idempotency key follows expected format."""
        key = scheduler.get_node_idempotency_key(test_task, "node1")
        
        # Should start with "req_"
        assert key.startswith("req_")
        
        # Should contain task_id prefix
        assert test_task[:8] in key
        
        # Should contain node_key
        assert "node1" in key
    
    def test_node_info_includes_idempotency_key(self, scheduler, test_task):
        """Test that node info includes idempotency key."""
        node_info = scheduler.get_node_info(test_task, "node1")
        
        assert node_info is not None
        assert "idempotency_key" in node_info
        assert node_info["idempotency_key"] is not None
        assert node_info["idempotency_key"].startswith("req_")
