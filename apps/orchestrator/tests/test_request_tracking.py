"""Tests for request tracking service (P36.1 idempotency)."""

from __future__ import annotations

import uuid

import pytest

# Import request tracking service
try:
    from request_tracking import RequestTrackingService, get_request_tracking_service
    REQUEST_TRACKING_AVAILABLE = True
except ImportError:
    REQUEST_TRACKING_AVAILABLE = False
    pytest.skip("Request tracking not available", allow_module_level=True)


@pytest.fixture
def tracking_service(database_url):
    """Create a request tracking service for testing."""
    if not REQUEST_TRACKING_AVAILABLE:
        pytest.skip("Request tracking not available")
    return RequestTrackingService(database_url=database_url)


@pytest.fixture
def sample_agent_id(database_url):
    """Create a sample agent in database and return its ID."""
    import psycopg
    from psycopg.rows import dict_row
    
    agent_id = str(uuid.uuid4())
    tenant_id = "00000000-0000-0000-0000-000000000001"
    agent_key = f"test-agent-{agent_id[:8]}"
    
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO agents (
                  id, tenant_id, agent_key, name, protocol, status, 
                  created_at, updated_at
                ) VALUES (
                  %s::uuid, %s::uuid, %s, %s, %s, %s, now(), now()
                )
                """,
                (
                    agent_id,
                    tenant_id,
                    agent_key,
                    "Test Agent",
                    "A2A",
                    "active",
                ),
            )
    
    yield agent_id
    
    # Cleanup
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM agents WHERE id = %s::uuid", (agent_id,))


@pytest.fixture
def sample_task_id(database_url):
    """Create a sample task in database and return its ID."""
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    
    task_id = str(uuid.uuid4())
    tenant_id = "00000000-0000-0000-0000-000000000001"
    
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO tasks (
                  id, tenant_id, title, input_json, status, progress,
                  plan_json, created_at, updated_at, started_at
                ) VALUES (
                  %s::uuid, %s::uuid, %s, %s::jsonb, 'created', 0,
                  %s::jsonb, now(), now(), now()
                )
                """,
                (
                    task_id,
                    tenant_id,
                    "Test task for request tracking",
                    Jsonb({"type": "text", "content": "test"}),
                    Jsonb({"nodes": []}),
                ),
            )
    
    yield task_id
    
    # Cleanup
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            conn.execute("DELETE FROM a2a_requests WHERE task_id = %s::uuid", (task_id,))
            conn.execute("DELETE FROM task_nodes WHERE task_id = %s::uuid", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = %s::uuid", (task_id,))


@pytest.fixture
def sample_node_id(sample_task_id, database_url):
    """Create a sample node in database and return its ID."""
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    
    node_id = str(uuid.uuid4())
    node_key = "test-node"
    
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO task_nodes (
                  id, task_id, node_key, agent_key, status, started_at, created_at, updated_at
                ) VALUES (
                  %s::uuid, %s::uuid, %s, %s, 'pending', now(), now(), now()
                )
                """,
                (node_id, sample_task_id, node_key, "claude-code"),
            )
    
    return node_id


@pytest.fixture
def sample_agent_id(database_url):
    """Create a sample agent in database and return its ID."""
    import psycopg
    from psycopg.rows import dict_row
    
    agent_id = str(uuid.uuid4())
    tenant_id = "00000000-0000-0000-0000-000000000001"
    
    with psycopg.connect(database_url, row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO agents (
                  id, tenant_id, agent_key, name, protocol, status, 
                  created_at, updated_at
                ) VALUES (
                  %s::uuid, %s::uuid, %s, %s, %s, %s, now(), now()
                )
                """,
                (
                    agent_id,
                    tenant_id,
                    "test-agent",
                    "Test Agent",
                    "A2A",
                    "active",
                ),
            )
    
    return agent_id


class TestRequestTrackingService:
    """Test request tracking service functionality."""

    def test_generate_idempotency_key(self, tracking_service):
        """Test idempotency key generation."""
        key1 = tracking_service.generate_idempotency_key()
        key2 = tracking_service.generate_idempotency_key()
        
        assert key1 != key2
        assert key1.startswith("req_")
        assert key2.startswith("req_")

    def test_track_new_request(
        self,
        tracking_service,
        sample_task_id,
        sample_node_id,
        sample_agent_id,
    ):
        """Test tracking a new request."""
        idempotency_key = tracking_service.generate_idempotency_key()
        request_json = {"query": "test query", "agent_key": "claude-code"}
        
        result = tracking_service.track_request(
            idempotency_key,
            sample_task_id,
            sample_node_id,
            sample_agent_id,
            request_json,
        )
        
        assert result is not None
        assert result["status"] == "pending"
        assert "id" in result
        assert "created_at" in result

    def test_track_duplicate_request(
        self,
        tracking_service,
        sample_task_id,
        sample_node_id,
        sample_agent_id,
    ):
        """Test tracking a duplicate request returns existing."""
        idempotency_key = tracking_service.generate_idempotency_key()
        request_json = {"query": "test query", "agent_key": "claude-code"}
        
        # First request
        result1 = tracking_service.track_request(
            idempotency_key,
            sample_task_id,
            sample_node_id,
            sample_agent_id,
            request_json,
        )
        
        # Duplicate request
        result2 = tracking_service.track_request(
            idempotency_key,
            sample_task_id,
            sample_node_id,
            sample_agent_id,
            request_json,
        )
        
        assert result1["status"] == "pending"
        assert result2["status"] == result1["status"]

    def test_get_request(self, tracking_service, sample_task_id, sample_node_id, sample_agent_id):
        """Test getting an existing request."""
        idempotency_key = tracking_service.generate_idempotency_key()
        request_json = {"query": "test query", "agent_key": "claude-code"}
        
        # Track request
        tracking_service.track_request(
            idempotency_key,
            sample_task_id,
            sample_node_id,
            sample_agent_id,
            request_json,
        )
        
        # Get request
        request = tracking_service.get_request(idempotency_key)
        
        assert request is not None
        assert request["idempotency_key"] == idempotency_key
        assert request["task_id"] == sample_task_id
        assert request["node_id"] == sample_node_id
        assert request["agent_id"] == sample_agent_id

    def test_mark_request_running(self, tracking_service, sample_task_id, sample_node_id, sample_agent_id):
        """Test marking a request as running."""
        idempotency_key = tracking_service.generate_idempotency_key()
        request_json = {"query": "test query", "agent_key": "claude-code"}
        
        # Track request
        tracking_service.track_request(
            idempotency_key,
            sample_task_id,
            sample_node_id,
            sample_agent_id,
            request_json,
        )
        
        # Mark as running
        success = tracking_service.mark_request_running(idempotency_key)
        
        assert success is True
        
        # Verify status
        request = tracking_service.get_request(idempotency_key)
        assert request["status"] == "running"

    def test_mark_request_completed(self, tracking_service, sample_task_id, sample_node_id, sample_agent_id):
        """Test marking a request as completed."""
        idempotency_key = tracking_service.generate_idempotency_key()
        request_json = {"query": "test query", "agent_key": "claude-code"}
        response_json = {"result": "test result", "artifacts": []}
        
        # Track request
        tracking_service.track_request(
            idempotency_key,
            sample_task_id,
            sample_node_id,
            sample_agent_id,
            request_json,
        )
        
        # Mark as completed
        success = tracking_service.mark_request_completed(idempotency_key, response_json)
        
        assert success is True
        
        # Verify status and response
        request = tracking_service.get_request(idempotency_key)
        assert request["status"] == "completed"
        assert request["response_json"] is not None
        assert request["finished_at"] is not None

    def test_mark_request_failed(self, tracking_service, sample_task_id, sample_node_id, sample_agent_id):
        """Test marking a request as failed."""
        idempotency_key = tracking_service.generate_idempotency_key()
        request_json = {"query": "test query", "agent_key": "claude-code"}
        error_message = "Test error"
        
        # Track request
        tracking_service.track_request(
            idempotency_key,
            sample_task_id,
            sample_node_id,
            sample_agent_id,
            request_json,
        )
        
        # Mark as failed
        success = tracking_service.mark_request_failed(idempotency_key, error_message)
        
        assert success is True
        
        # Verify status and error
        request = tracking_service.get_request(idempotency_key)
        assert request["status"] == "failed"
        assert request["error_message"] == error_message
        assert request["finished_at"] is not None

    def test_is_request_completed(self, tracking_service, sample_task_id, sample_node_id, sample_agent_id):
        """Test checking if a request is completed."""
        idempotency_key = tracking_service.generate_idempotency_key()
        request_json = {"query": "test query", "agent_key": "claude-code"}
        response_json = {"result": "test result"}
        
        # Not completed initially
        assert tracking_service.is_request_completed(idempotency_key) is False
        
        # Track and complete request
        tracking_service.track_request(
            idempotency_key,
            sample_task_id,
            sample_node_id,
            sample_agent_id,
            request_json,
        )
        tracking_service.mark_request_completed(idempotency_key, response_json)
        
        # Now completed
        assert tracking_service.is_request_completed(idempotency_key) is True

    def test_get_cached_response(self, tracking_service, sample_task_id, sample_node_id, sample_agent_id):
        """Test getting cached response for completed request."""
        idempotency_key = tracking_service.generate_idempotency_key()
        request_json = {"query": "test query", "agent_key": "claude-code"}
        response_json = {"result": "test result", "artifacts": [{"name": "test"}]}
        
        # Track and complete request
        tracking_service.track_request(
            idempotency_key,
            sample_task_id,
            sample_node_id,
            sample_agent_id,
            request_json,
        )
        tracking_service.mark_request_completed(idempotency_key, response_json)
        
        # Get cached response
        cached = tracking_service.get_cached_response(idempotency_key)
        
        assert cached is not None
        assert cached["result"] == "test result"
        assert cached["artifacts"][0]["name"] == "test"

    def test_nonexistent_request(self, tracking_service):
        """Test operations on nonexistent request."""
        idempotency_key = "nonexistent_key"
        
        assert tracking_service.get_request(idempotency_key) is None
        assert tracking_service.is_request_completed(idempotency_key) is False
        assert tracking_service.get_cached_response(idempotency_key) is None
        assert tracking_service.mark_request_running(idempotency_key) is False
        assert tracking_service.mark_request_completed(idempotency_key, {}) is False
        assert tracking_service.mark_request_failed(idempotency_key, "error") is False


class TestRequestTrackingServiceSingleton:
    """Test request tracking service singleton."""

    def test_get_singleton(self):
        """Test getting singleton instance."""
        service1 = get_request_tracking_service()
        service2 = get_request_tracking_service()
        
        assert service1 is service2
