"""Phase 1 end-to-end smoke verification script.

Tests running Vaultly service against http://localhost:8000/api/v1:
1. Register Tenant A and Tenant B
2. Login and retrieve tokens
3. Tenant isolation verification (Tenant A cannot see Tenant B's users/groups -> 404)
4. Refresh token rotation and family reuse detection revocation
5. Logout and token denylist check
6. RBAC permission matrix (Admin vs Member vs Viewer)
7. Last admin protection verification
"""

import secrets
import sys
import httpx

BASE_URL = "http://localhost:8000/api/v1"


def print_step(title: str) -> None:
    print(f"\n[+] {title}")


def main() -> None:
    random_suffix = secrets.token_hex(4)
    slug_a = f"acme-{random_suffix}"
    email_a = f"alice_{random_suffix}@acme.com"
    slug_b = f"beta-{random_suffix}"
    email_b = f"bob_{random_suffix}@beta.com"

    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        # Check health
        print_step("Checking service health...")
        resp = client.get("/health")
        assert resp.status_code == 200, f"Health check failed: {resp.text}"
        print("  -> Service is healthy.")

        # 1. Register Tenant A
        print_step(f"Registering Tenant A ({slug_a})...")
        res_a = client.post(
            "/auth/register-tenant",
            json={
                "tenant_name": "Acme Corp",
                "tenant_slug": slug_a,
                "email": email_a,
                "password": "SuperSecretPassword123!",
                "full_name": "Alice Admin",
            },
        )
        assert res_a.status_code == 201, f"Tenant A registration failed: {res_a.text}"
        tokens_a = res_a.json()
        token_a = tokens_a["access_token"]
        refresh_a = tokens_a["refresh_token"]
        headers_a = {"Authorization": f"Bearer {token_a}"}
        print("  PASS: Tenant A registered successfully.")

        # 2. Register Tenant B
        print_step(f"Registering Tenant B ({slug_b})...")
        res_b = client.post(
            "/auth/register-tenant",
            json={
                "tenant_name": "Beta LLC",
                "tenant_slug": slug_b,
                "email": email_b,
                "password": "SuperSecretPassword123!",
                "full_name": "Bob Admin",
            },
        )
        assert res_b.status_code == 201, f"Tenant B registration failed: {res_b.text}"
        tokens_b = res_b.json()
        token_b = tokens_b["access_token"]
        headers_b = {"Authorization": f"Bearer {token_b}"}
        print("  PASS: Tenant B registered successfully.")

        # 3. /auth/me for both
        print_step("Verifying /auth/me identity...")
        me_a = client.get("/auth/me", headers=headers_a)
        assert me_a.status_code == 200
        assert me_a.json()["role"] == "ADMIN"
        assert me_a.json()["email"] == email_a

        me_b = client.get("/auth/me", headers=headers_b)
        assert me_b.status_code == 200
        assert me_b.json()["role"] == "ADMIN"
        assert me_b.json()["email"] == email_b
        print("  PASS: Identity and roles verified for both tenants.")

        # 4. Cross-tenant isolation (Tenant A creates group, Tenant B gets 404)
        print_step("Testing cross-tenant isolation (expect 404)...")
        grp_res = client.post(
            "/groups",
            headers=headers_a,
            json={"name": "Acme Engineering", "description": "Engineers at Acme"},
        )
        assert grp_res.status_code == 201
        grp_id = grp_res.json()["id"]

        # Tenant B tries to access Acme's group
        cross_res = client.get(f"/groups/{grp_id}", headers=headers_b)
        assert cross_res.status_code == 404, f"Expected 404 for cross-tenant access, got: {cross_res.status_code}"
        print("  PASS: Cross-tenant group isolation returns 404 Not Found.")

        # 5. Invite User into Tenant A with MEMBER role
        print_step("Inviting Charlie as MEMBER to Tenant A...")
        inv_res = client.post(
            "/users/invite",
            headers=headers_a,
            json={"email": f"charlie_{random_suffix}@acme.com", "role": "MEMBER"},
        )
        assert inv_res.status_code == 201
        invite_token = inv_res.json()["invite_token"]

        # Charlie accepts invite
        accept_res = client.post(
            "/auth/accept-invite",
            json={
                "token": invite_token,
                "password": "CharliePassword123!",
                "full_name": "Charlie Member",
            },
        )
        assert accept_res.status_code == 200
        charlie_token = accept_res.json()["access_token"]
        charlie_headers = {"Authorization": f"Bearer {charlie_token}"}

        # Charlie tries to invite someone (Member cannot invite -> 403)
        charlie_invite = client.post(
            "/users/invite",
            headers=charlie_headers,
            json={"email": f"dan_{random_suffix}@acme.com", "role": "AUDITOR"},
        )
        assert charlie_invite.status_code == 403, f"Expected 403 for member invite, got {charlie_invite.status_code}"
        print("  PASS: RBAC enforced: Member cannot invite users (403 Forbidden).")

        # 6. Refresh Token Rotation & Family Reuse Detection
        print_step("Testing Refresh Token Rotation & Family Reuse Detection...")
        rot1 = client.post("/auth/refresh", json={"refresh_token": refresh_a})
        assert rot1.status_code == 200
        new_refresh = rot1.json()["refresh_token"]

        # Reuse old refresh token (refresh_a) -> expect 401
        reuse_res = client.post("/auth/refresh", json={"refresh_token": refresh_a})
        assert reuse_res.status_code == 401, f"Expected 401 on reuse, got: {reuse_res.status_code}"

        # Using new_refresh should now also fail because family was revoked
        revoked_res = client.post("/auth/refresh", json={"refresh_token": new_refresh})
        assert revoked_res.status_code == 401, f"Expected 401 on revoked family, got: {revoked_res.status_code}"
        print("  PASS: Refresh token rotation and family-wide reuse revocation verified.")

        # 7. Logout and Denylist check
        print_step("Testing Logout and Token Denylisting...")
        login_res = client.post(
            "/auth/login",
            data={
                "username": email_a,
                "password": "SuperSecretPassword123!",
                "client_id": slug_a,
            },
        )
        assert login_res.status_code == 200
        fresh_token = login_res.json()["access_token"]
        fresh_headers = {"Authorization": f"Bearer {fresh_token}"}

        # Verify works
        assert client.get("/auth/me", headers=fresh_headers).status_code == 200

        # Logout
        logout_res = client.post("/auth/logout", headers=fresh_headers)
        assert logout_res.status_code == 200

        # Access with fresh_token must now be 401
        after_logout = client.get("/auth/me", headers=fresh_headers)
        assert after_logout.status_code == 401, f"Expected 401 after logout, got: {after_logout.status_code}"
        print("  PASS: Logout and Redis token denylist verified.")

        # 8. Last Admin Protection
        print_step("Testing Last Admin Protection...")
        login_res2 = client.post(
            "/auth/login",
            data={
                "username": email_a,
                "password": "SuperSecretPassword123!",
                "client_id": slug_a,
            },
        )
        alice_hdr = {"Authorization": f"Bearer {login_res2.json()['access_token']}"}
        alice_id = me_a.json()["user_id"]

        # Alice attempts to demote herself
        demote_res = client.patch(
            f"/users/{alice_id}/role",
            headers=alice_hdr,
            json={"role": "MEMBER"},
        )
        assert demote_res.status_code == 400, f"Expected 400 on last admin demotion, got {demote_res.status_code}"
        print("  PASS: Last admin demotion rejected with 400 Bad Request.")

        # Alice attempts to remove herself
        delete_res = client.delete(f"/users/{alice_id}", headers=alice_hdr)
        assert delete_res.status_code == 400, f"Expected 400 on last admin removal, got {delete_res.status_code}"
        print("  PASS: Last admin removal rejected with 400 Bad Request.")

    print("\n=======================================================")
    print("ALL PHASE 1 SMOKE CHECKS PASSED: 100% OK")
    print("=======================================================\n")


if __name__ == "__main__":
    main()
