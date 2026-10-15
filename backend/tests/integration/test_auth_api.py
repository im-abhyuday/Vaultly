"""Integration tests for Phase 1B: Auth, RBAC, Tokens, and Isolation.

Tests registration, OAuth2 login, token refresh rotation and reuse detection,
logout denylisting, RBAC matrix, and cross-tenant resource 404 boundaries.
"""


import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_tenant_registration_and_login() -> None:
    """Register tenant and log in with OAuth2 password flow."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Register tenant
        reg_payload = {
            "tenant_name": "Acme Corp",
            "tenant_slug": "acme-corp",
            "email": "admin@acme.com",
            "password": "SecurePassword123!",
            "full_name": "Acme Admin",
        }
        res = await client.post("/api/v1/auth/register-tenant", json=reg_payload)
        assert res.status_code == 201
        data = res.json()
        assert "access_token" in data
        assert "refresh_token" in data

        # Duplicate slug rejected with 409
        dup_res = await client.post("/api/v1/auth/register-tenant", json=reg_payload)
        assert dup_res.status_code == 409

        # 2. Login with OAuth2 form data
        login_data = {
            "username": "admin@acme.com",
            "password": "SecurePassword123!",
            "client_id": "acme-corp",
        }
        login_res = await client.post("/api/v1/auth/login", data=login_data)
        assert login_res.status_code == 200
        token_data = login_res.json()
        access_tok = token_data["access_token"]
        refresh_tok = token_data["refresh_token"]
        assert len(refresh_tok) > 10

        # 3. Access /auth/me
        headers = {"Authorization": f"Bearer {access_tok}"}
        me_res = await client.get("/api/v1/auth/me", headers=headers)
        assert me_res.status_code == 200
        profile = me_res.json()
        assert profile["email"] == "admin@acme.com"
        assert profile["role"] == "ADMIN"
        assert profile["tenant_slug"] == "acme-corp"


@pytest.mark.asyncio
async def test_cross_tenant_isolation_404_responses() -> None:
    """Tenant A admin cannot view, patch, or delete Tenant B's resources, receiving 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Register Tenant A
        reg_a = {
            "tenant_name": "Company A",
            "tenant_slug": "company-a",
            "email": "admin@companya.com",
            "password": "Password12345!",
            "full_name": "Admin A",
        }
        res_a = await client.post("/api/v1/auth/register-tenant", json=reg_a)
        tok_a = res_a.json()["access_token"]
        headers_a = {"Authorization": f"Bearer {tok_a}"}

        # Register Tenant B
        reg_b = {
            "tenant_name": "Company B",
            "tenant_slug": "company-b",
            "email": "admin@companyb.com",
            "password": "Password12345!",
            "full_name": "Admin B",
        }
        res_b = await client.post("/api/v1/auth/register-tenant", json=reg_b)
        tok_b = res_b.json()["access_token"]
        headers_b = {"Authorization": f"Bearer {tok_b}"}

        # Tenant B creates a group
        grp_res = await client.post(
            "/api/v1/groups",
            json={"name": "Secret Beta Group", "description": "Classified"},
            headers=headers_b,
        )
        assert grp_res.status_code == 201
        gid_b = grp_res.json()["id"]

        # Tenant A tries to GET Tenant B's group -> 404 (not 403)
        get_res = await client.get(f"/api/v1/groups/{gid_b}", headers=headers_a)
        assert get_res.status_code == 404

        # Tenant A tries to PATCH Tenant B's group -> 404
        patch_res = await client.patch(
            f"/api/v1/groups/{gid_b}",
            json={"name": "Tampered"},
            headers=headers_a,
        )
        assert patch_res.status_code == 404

        # Tenant A tries to DELETE Tenant B's group -> 404
        del_res = await client.delete(f"/api/v1/groups/{gid_b}", headers=headers_a)
        assert del_res.status_code == 404

        # Tenant A tries to add a user from Tenant B to Tenant A group -> 404
        grp_a_res = await client.post(
            "/api/v1/groups",
            json={"name": "Alpha Group"},
            headers=headers_a,
        )
        gid_a = grp_a_res.json()["id"]

        user_b_me = (await client.get("/api/v1/auth/me", headers=headers_b)).json()
        uid_b = user_b_me["user_id"]

        add_cross_res = await client.post(
            f"/api/v1/groups/{gid_a}/members",
            json={"user_id": uid_b},
            headers=headers_a,
        )
        assert add_cross_res.status_code == 404


@pytest.mark.asyncio
async def test_refresh_token_rotation_and_family_reuse_detection() -> None:
    """Refresh token rotates on use; reusing an old token revokes the entire family."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Register tenant
        reg = {
            "tenant_name": "Rotation Org",
            "tenant_slug": "rotation-org",
            "email": "user@rotation.com",
            "password": "Password12345!",
            "full_name": "Rotation User",
        }
        res = await client.post("/api/v1/auth/register-tenant", json=reg)
        tok1 = res.json()["refresh_token"]

        # 1. First rotation -> returns tok2
        ref1_res = await client.post("/api/v1/auth/refresh", json={"refresh_token": tok1})
        assert ref1_res.status_code == 200
        tok2 = ref1_res.json()["refresh_token"]
        assert tok2 != tok1

        # 2. Second rotation -> returns tok3
        ref2_res = await client.post("/api/v1/auth/refresh", json={"refresh_token": tok2})
        assert ref2_res.status_code == 200
        tok3 = ref2_res.json()["refresh_token"]
        assert tok3 != tok2

        # 3. REUSE DETECTION: Re-submit old tok1 -> Family revocation triggered!
        reuse_res = await client.post("/api/v1/auth/refresh", json={"refresh_token": tok1})
        assert reuse_res.status_code == 401
        assert "reuse detected" in reuse_res.json()["detail"].lower()

        # 4. Attempt to use latest tok3 -> Also rejected because family was revoked!
        post_rev_res = await client.post("/api/v1/auth/refresh", json={"refresh_token": tok3})
        assert post_rev_res.status_code == 401


@pytest.mark.asyncio
async def test_logout_and_token_denylist() -> None:
    """Logout invalidates access token in Redis and revokes refresh token family."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        reg = {
            "tenant_name": "Logout Corp",
            "tenant_slug": "logout-corp",
            "email": "admin@logout.com",
            "password": "Password12345!",
            "full_name": "Logout Admin",
        }
        res = await client.post("/api/v1/auth/register-tenant", json=reg)
        access_tok = res.json()["access_token"]
        refresh_tok = res.json()["refresh_token"]
        headers = {"Authorization": f"Bearer {access_tok}"}

        # Logout
        logout_res = await client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": refresh_tok},
            headers=headers,
        )
        assert logout_res.status_code == 200

        # Access token rejected
        me_res = await client.get("/api/v1/auth/me", headers=headers)
        assert me_res.status_code == 401

        # Refresh token rejected
        ref_res = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_tok})
        assert ref_res.status_code == 401


@pytest.mark.asyncio
async def test_rbac_matrix_and_last_admin_protection() -> None:
    """Verify ADMIN, MEMBER, AUDITOR role permissions and last-admin guard."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. Register admin
        reg = {
            "tenant_name": "RBAC Org",
            "tenant_slug": "rbac-org",
            "email": "admin@rbac.com",
            "password": "Password12345!",
            "full_name": "RBAC Admin",
        }
        admin_res = await client.post("/api/v1/auth/register-tenant", json=reg)
        admin_tok = admin_res.json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_tok}"}

        # 2. Invite a member
        inv_res = await client.post(
            "/api/v1/users/invite",
            json={"email": "member@rbac.com", "role": "MEMBER"},
            headers=admin_headers,
        )
        assert inv_res.status_code == 201
        member_invite_token = inv_res.json()["invite_token"]

        # Accept invite as member
        acc_res = await client.post(
            "/api/v1/auth/accept-invite",
            json={
                "token": member_invite_token,
                "password": "Password12345!",
                "full_name": "RBAC Member",
            },
        )
        assert acc_res.status_code == 200
        member_tok = acc_res.json()["access_token"]
        member_headers = {"Authorization": f"Bearer {member_tok}"}

        # 3. Invite an auditor
        inv_aud_res = await client.post(
            "/api/v1/users/invite",
            json={"email": "auditor@rbac.com", "role": "AUDITOR"},
            headers=admin_headers,
        )
        assert inv_aud_res.status_code == 201
        auditor_invite_token = inv_aud_res.json()["invite_token"]

        acc_aud_res = await client.post(
            "/api/v1/auth/accept-invite",
            json={
                "token": auditor_invite_token,
                "password": "Password12345!",
                "full_name": "RBAC Auditor",
            },
        )
        assert acc_aud_res.status_code == 200
        auditor_tok = acc_aud_res.json()["access_token"]
        auditor_headers = {"Authorization": f"Bearer {auditor_tok}"}

        # 4. Check permissions on GET /users
        # Admin can view
        assert (await client.get("/api/v1/users", headers=admin_headers)).status_code == 200
        # Auditor can view
        assert (await client.get("/api/v1/users", headers=auditor_headers)).status_code == 200
        # Member is forbidden (403)
        assert (await client.get("/api/v1/users", headers=member_headers)).status_code == 403

        # 5. Check permissions on Group creation (POST /groups)
        # Admin can create
        grp_create = await client.post(
            "/api/v1/groups",
            json={"name": "Team One"},
            headers=admin_headers,
        )
        assert grp_create.status_code == 201
        gid = grp_create.json()["id"]

        # Member cannot create group (403)
        assert (await client.post("/api/v1/groups", json={"name": "Team Member"}, headers=member_headers)).status_code == 403
        # Auditor cannot create group (403 - read only everywhere)
        assert (await client.post("/api/v1/groups", json={"name": "Team Aud"}, headers=auditor_headers)).status_code == 403

        # Both Member and Auditor CAN view groups (GET /groups)
        assert (await client.get("/api/v1/groups", headers=member_headers)).status_code == 200
        assert (await client.get("/api/v1/groups", headers=auditor_headers)).status_code == 200
        assert (await client.get(f"/api/v1/groups/{gid}", headers=member_headers)).status_code == 200
        assert (await client.get(f"/api/v1/groups/{gid}", headers=auditor_headers)).status_code == 200

        # 6. Last Admin Protection
        # Try to demote the admin -> 400 Bad Request
        admin_me = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()
        admin_uid = admin_me["user_id"]

        demote_res = await client.patch(
            f"/api/v1/users/{admin_uid}/role",
            json={"role": "MEMBER"},
            headers=admin_headers,
        )
        assert demote_res.status_code == 400
        assert "last remaining admin" in demote_res.json()["detail"].lower()

        # Try to delete the admin -> 400 Bad Request
        del_admin_res = await client.delete(
            f"/api/v1/users/{admin_uid}",
            headers=admin_headers,
        )
        assert del_admin_res.status_code == 400
        assert "last remaining admin" in del_admin_res.json()["detail"].lower()
