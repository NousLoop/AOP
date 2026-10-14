package registry

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

func testDB(t *testing.T) *pgxpool.Pool {
	t.Helper()
	url := os.Getenv("DATABASE_URL")
	if url == "" {
		url = "postgresql://aop:aop@127.0.0.1:5432/aop"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Skipf("postgres unavailable: %v", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		t.Skipf("postgres ping failed: %v", err)
	}
	// Require migration columns.
	var hasIdentity bool
	err = pool.QueryRow(ctx, `
		SELECT EXISTS (
		  SELECT 1 FROM information_schema.columns
		  WHERE table_name = 'agents' AND column_name = 'identity'
		)
	`).Scan(&hasIdentity)
	if err != nil || !hasIdentity {
		pool.Close()
		t.Skip("agents.identity column missing; run migrate.py")
	}
	t.Cleanup(pool.Close)
	return pool
}

func TestRegisterDefaultsAndPreserveMeta(t *testing.T) {
	pool := testDB(t)
	store := &Store{DB: pool}
	ctx := context.Background()
	tenant := defaultTenantID
	key := "test-meta-agent"
	endpoint := "http://127.0.0.1:18099/"

	_, _ = pool.Exec(ctx, `DELETE FROM agents WHERE tenant_id = $1::uuid AND agent_key = $2`, tenant, key)

	card := AgentCard{
		Name:        "Test Meta Agent",
		Description: "meta test",
		URL:         endpoint,
		Version:     "0.0.1",
	}
	raw, _ := json.Marshal(card)

	res, err := store.Register(ctx, tenant, endpoint, card, raw)
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	rec, err := store.Get(ctx, tenant, res.AgentID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if rec.Identity != DefaultIdentity {
		t.Fatalf("default identity = %q", rec.Identity)
	}
	if rec.OwnerUserID == "" {
		t.Fatal("expected default owner (admin) when seeded")
	}
	ownerBefore := rec.OwnerUserID

	manager := IdentityManager
	updated, err := store.UpdateMeta(ctx, tenant, rec.ID, &manager, nil)
	if err != nil {
		t.Fatalf("update meta: %v", err)
	}
	if updated.Identity != IdentityManager {
		t.Fatalf("identity after patch = %q", updated.Identity)
	}

	card.Description = "re-registered"
	raw2, _ := json.Marshal(card)
	if _, err := store.Register(ctx, tenant, endpoint, card, raw2); err != nil {
		t.Fatalf("re-register: %v", err)
	}
	again, err := store.Get(ctx, tenant, rec.ID)
	if err != nil {
		t.Fatalf("get after re-register: %v", err)
	}
	if again.Identity != IdentityManager {
		t.Fatalf("re-register overwrote identity: %q", again.Identity)
	}
	if again.OwnerUserID != ownerBefore {
		t.Fatalf("re-register overwrote owner: %q -> %q", ownerBefore, again.OwnerUserID)
	}

	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(),
			`DELETE FROM agents WHERE tenant_id = $1::uuid AND agent_key = $2`, tenant, key)
	})
}

func TestUpdateMetaValidation(t *testing.T) {
	pool := testDB(t)
	store := &Store{DB: pool}
	ctx := context.Background()
	tenant := defaultTenantID
	key := "test-meta-validation"
	endpoint := "http://127.0.0.1:18098/"

	_, _ = pool.Exec(ctx, `DELETE FROM agents WHERE tenant_id = $1::uuid AND agent_key = $2`, tenant, key)
	card := AgentCard{Name: "Test Meta Validation", URL: endpoint, Version: "0.0.1"}
	raw, _ := json.Marshal(card)
	res, err := store.Register(ctx, tenant, endpoint, card, raw)
	if err != nil {
		t.Fatalf("register: %v", err)
	}

	bad := "ceo"
	if _, err := store.UpdateMeta(ctx, tenant, res.AgentID, &bad, nil); !errors.Is(err, ErrInvalidIdentity) {
		t.Fatalf("expected ErrInvalidIdentity, got %v", err)
	}

	foreign := "00000000-0000-0000-0000-999999999999"
	if _, err := store.UpdateMeta(ctx, tenant, res.AgentID, nil, &foreign); !errors.Is(err, ErrInvalidOwner) {
		t.Fatalf("expected ErrInvalidOwner, got %v", err)
	}

	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(),
			`DELETE FROM agents WHERE tenant_id = $1::uuid AND agent_key = $2`, tenant, key)
	})
}
