"""
Phase 36 Execution Integration Test - Scheduler Level
Tests request tracking and outbox integration through scheduler directly
Bypasses API to test the core reliability integration
"""

import time
import uuid
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import sys
import os
import io

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Configuration
DATABASE_URL = "postgresql://aop:aop@127.0.0.1:5432/aop"

def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")

def test_request_tracking_direct():
    """Test request tracking service directly"""
    print_section("Request Tracking Direct Test (P36.1)")
    
    try:
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        from request_tracking import RequestTrackingService
        
        tracking = RequestTrackingService(database_url=DATABASE_URL)
        
        # Create test data
        task_id = str(uuid.uuid4())
        node_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        tenant_id = "00000000-0000-0000-0000-000000000001"
        
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            with conn.transaction():
                # Create agent
                conn.execute(
                    """
                    INSERT INTO agents (id, tenant_id, agent_key, name, protocol, status, created_at, updated_at)
                    VALUES (%s::uuid, %s::uuid, %s, %s, %s, %s, now(), now())
                    """,
                    (agent_id, tenant_id, f"test-agent-{agent_id[:8]}", "Test Agent", "A2A", "active"),
                )
                
                # Create task
                conn.execute(
                    """
                    INSERT INTO tasks (id, tenant_id, title, input_json, status, progress, plan_json, created_at, updated_at, started_at)
                    VALUES (%s::uuid, %s::uuid, %s, %s::jsonb, 'created', 0, %s::jsonb, now(), now(), now())
                    """,
                    (task_id, tenant_id, "Test Task", Jsonb({"type": "text", "content": "test"}), Jsonb({"nodes": []})),
                )
                
                # Create node with idempotency key
                node_key = "test-node"
                idempotency_key = f"req_{task_id[:8]}_{node_key}"
                conn.execute(
                    """
                    INSERT INTO task_nodes (id, task_id, node_key, agent_key, status, input_json, attempt, max_retry, idempotency_key, created_at, updated_at)
                    VALUES (%s::uuid, %s::uuid, %s, %s, 'pending', %s::jsonb, 0, 3, %s, now(), now())
                    """,
                    (node_id, task_id, node_key, "claude-code", Jsonb({"goal": "test"}), idempotency_key),
                )
        
        # Test request tracking
        print("[INFO] Tracking request...")
        result = tracking.track_request(
            idempotency_key,
            task_id,
            node_id,
            agent_id,
            {"query": "test query"},
        )
        
        if result and result.get("idempotency_key") == idempotency_key:
            print(f"[OK] Request tracked successfully")
            print(f"   - Request ID: {result.get('id')}")
            print(f"   - Idempotency Key: {result.get('idempotency_key')}")
            print(f"   - Status: {result.get('status')}")
            
            # Test completion
            print("[INFO] Marking request as completed...")
            completed = tracking.mark_request_completed(idempotency_key, {"result": "test result"})
            
            if completed:
                print(f"[OK] Request marked as completed")
                
                # Verify completion
                is_completed = tracking.is_request_completed(idempotency_key)
                if is_completed:
                    print(f"[OK] Request completion verified")
                    
                    # Test cached response
                    cached = tracking.get_cached_response(idempotency_key)
                    if cached:
                        print(f"[OK] Cached response retrieved: {cached}")
                    else:
                        print(f"[FAIL] Cached response not found")
                        return False
                else:
                    print(f"[FAIL] Request completion not verified")
                    return False
            else:
                print(f"[FAIL] Request completion failed")
                return False
            
            # Test duplicate detection
            print("[INFO] Testing duplicate detection...")
            result2 = tracking.track_request(
                idempotency_key,
                task_id,
                node_id,
                agent_id,
                {"query": "test query"},
            )
            
            if result2 and result2.get("id") == result.get("id"):
                print(f"[OK] Duplicate detection working - returned same request")
            else:
                print(f"[FAIL] Duplicate detection failed")
                return False
            
            # Cleanup
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.transaction():
                    conn.execute("DELETE FROM a2a_requests WHERE idempotency_key = %s", (idempotency_key,))
                    conn.execute("DELETE FROM task_nodes WHERE task_id = %s::uuid", (task_id,))
                    conn.execute("DELETE FROM tasks WHERE id = %s::uuid", (task_id,))
                    conn.execute("DELETE FROM agents WHERE id = %s::uuid", (agent_id,))
            
            return True
        else:
            print(f"[FAIL] Request tracking failed")
            return False
            
    except Exception as e:
        print(f"[FAIL] Request tracking direct test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    """Run scheduler-level integration tests"""
    print_section("Phase 36 Scheduler Integration Test")
    print("Testing: Request Tracking -> Completion -> Cache -> Duplicate Detection")
    print("Focus: Core Reliability Integration\n")
    
    results = []
    
    # Test 1: Request tracking direct
    results.append(("Request Tracking Direct", test_request_tracking_direct()))
    
    # Summary
    print_section("Test Summary")
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "[PASS]" if result else "[FAIL]"
        print(f"{status}: {test_name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n[SUCCESS] All tests passed! Phase 36 request tracking integration is operational.")
    else:
        print(f"\n[WARNING] {total - passed} test(s) failed. Review the output above for details.")

if __name__ == "__main__":
    main()
