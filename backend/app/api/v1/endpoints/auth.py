"""Authentication API endpoints.

Handles tenant registration, OAuth2 password login, token refresh, logout,
profile lookup, and invitation acceptance.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    get_current_session,
    get_current_user,
)
from app.core.config import get_settings
from app.core.security import (
    create_access_token,
    denylist_access_token,
    generate_opaque_token,
    hash_password,
    hash_token_secret,
    parse_prefixed_token,
    verify_dummy_password,
    verify_password,
)
from app.db.session import async_session_maker, set_tenant_context
from app.models.enums import RoleEnum
from app.models.invitation import Invitation
from app.models.membership import Membership
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.auth import (
    AcceptInviteRequest,
    RefreshTokenRequest,
    RegisterTenantRequest,
    TokenResponse,
    UserProfileResponse,
)

router = APIRouter()
settings = get_settings()


@router.post("/register-tenant", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register_tenant(payload: RegisterTenantRequest) -> TokenResponse:
    """Register a new tenant, create the first user, and grant ADMIN role in one transaction."""
    async with async_session_maker() as session:
        async with session.begin():
            # Check for slug collision via SECURITY DEFINER function (before tenant context is set)
            existing_tid = (
                await session.execute(
                    select(func.resolve_tenant_id(payload.tenant_slug))
                )
            ).scalar()
            if existing_tid:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Tenant slug '{payload.tenant_slug}' is already taken.",
                )

            # Generate Tenant UUID in app and set transaction-local tenant context
            tenant_id = uuid.uuid4()
            await set_tenant_context(session, tenant_id)

            tenant = Tenant(
                id=tenant_id,
                name=payload.tenant_name,
                slug=payload.tenant_slug,
                is_active=True,
            )
            session.add(tenant)
            await session.flush()

            # Find or create global user identity
            user_stmt = select(User).where(func.lower(User.email) == payload.email.lower())
            user = (await session.execute(user_stmt)).scalar_one_or_none()
            if not user:
                user_id = uuid.uuid4()
                user = User(
                    id=user_id,
                    email=payload.email.lower(),
                    password_hash=hash_password(payload.password),
                    full_name=payload.full_name,
                    is_active=True,
                )
                session.add(user)
                await session.flush()

            # Create initial ADMIN membership
            membership = Membership(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user.id,
                role=RoleEnum.ADMIN.value,
                is_active=True,
            )
            session.add(membership)

            # Generate access and refresh tokens
            access_tok, jti, exp_ts = create_access_token(user.id, tenant_id)
            refresh_tok_str, refresh_hash = generate_opaque_token(tenant_id)

            refresh_token = RefreshToken(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user.id,
                family_id=uuid.uuid4(),
                token_hash=refresh_hash,
                expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            )
            session.add(refresh_token)

    return TokenResponse(
        access_token=access_tok,
        refresh_token=refresh_tok_str,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> TokenResponse:
    """OAuth2 password login.

    username = email, password = password, client_id = tenant slug.
    """
    slug = form_data.client_id
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing client_id (tenant slug). Pass tenant slug as client_id in OAuth2 form.",
        )

    async with async_session_maker() as session:
        async with session.begin():
            # Resolve tenant ID via SECURITY DEFINER function before tenant context is set
            resolve_res = await session.execute(
                select(func.resolve_tenant_id(slug))
            )
            tenant_id = resolve_res.scalar()
            if not tenant_id:
                # Timing mitigation: dummy verify
                verify_dummy_password(form_data.password)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Set tenant context for this session
            await set_tenant_context(session, tenant_id)

            # Lookup global user
            user_stmt = select(User).where(func.lower(User.email) == form_data.username.lower())
            user = (await session.execute(user_stmt)).scalar_one_or_none()
            if not user or not user.is_active:
                verify_dummy_password(form_data.password)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Verify password
            if not verify_password(form_data.password, user.password_hash):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Verify membership
            mem_stmt = select(Membership).where(
                Membership.tenant_id == tenant_id,
                Membership.user_id == user.id,
                Membership.is_active == True,  # noqa: E712
            )
            membership = (await session.execute(mem_stmt)).scalar_one_or_none()
            if not membership:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid credentials.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Generate access and refresh tokens
            access_tok, jti, exp_ts = create_access_token(user.id, tenant_id)
            refresh_tok_str, refresh_hash = generate_opaque_token(tenant_id)

            refresh_token = RefreshToken(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user.id,
                family_id=uuid.uuid4(),
                token_hash=refresh_hash,
                expires_at=datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            )
            session.add(refresh_token)

    return TokenResponse(
        access_token=access_tok,
        refresh_token=refresh_tok_str,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_access_token(payload: RefreshTokenRequest) -> TokenResponse:
    """Rotate refresh token and issue a new access token with family-based reuse detection."""
    try:
        tenant_id, secret = parse_prefixed_token(payload.refresh_token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid refresh token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    token_hash = hash_token_secret(payload.refresh_token)

    async with async_session_maker() as session:
        async with session.begin():
            # Set tenant context from extracted prefix
            await set_tenant_context(session, tenant_id)

            # Locate token in DB
            stmt = select(RefreshToken).where(
                RefreshToken.tenant_id == tenant_id,
                RefreshToken.token_hash == token_hash,
            )
            tok_row = (await session.execute(stmt)).scalar_one_or_none()
            if not tok_row:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid refresh token.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            now = datetime.now(UTC)

            # Family Reuse Detection: If already revoked or replaced, revoke entire family
            if tok_row.revoked_at is not None or tok_row.replaced_by is not None:
                # Security incident: token reuse detected! Revoke whole family
                await session.execute(
                    update(RefreshToken)
                    .where(
                        RefreshToken.tenant_id == tenant_id,
                        RefreshToken.family_id == tok_row.family_id,
                    )
                    .values(revoked_at=now)
                )
                await session.commit()
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Refresh token reuse detected. Family has been revoked.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Check expiration
            if tok_row.expires_at <= now:
                tok_row.revoked_at = now
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Refresh token has expired.",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Generate replacement token in the same family
            new_refresh_str, new_refresh_hash = generate_opaque_token(tenant_id)
            new_tok_id = uuid.uuid4()
            new_tok_row = RefreshToken(
                id=new_tok_id,
                tenant_id=tenant_id,
                user_id=tok_row.user_id,
                family_id=tok_row.family_id,  # Preserve family
                token_hash=new_refresh_hash,
                expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            )
            session.add(new_tok_row)
            await session.flush()

            # Mark current token as revoked and replaced
            tok_row.revoked_at = now
            tok_row.replaced_by = new_tok_id
            await session.flush()

            access_tok, jti, exp_ts = create_access_token(tok_row.user_id, tenant_id)

    return TokenResponse(
        access_token=access_tok,
        refresh_token=new_refresh_str,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(
    payload: RefreshTokenRequest | None = None,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_current_session),
) -> dict[str, str]:
    """Logout current user: denylist access token in Redis and revoke refresh token family."""
    # 1. Denylist access token JTI for remaining lifetime
    exp_ts = int((datetime.now(UTC) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp())
    await denylist_access_token(current_user.token_jti, exp_ts)

    # 2. Revoke refresh token family if provided
    if payload and payload.refresh_token:
        try:
            tenant_id, secret = parse_prefixed_token(payload.refresh_token)
            if tenant_id == current_user.tenant_id:
                token_hash = hash_token_secret(payload.refresh_token)
                tok_row = (
                    await session.execute(
                        select(RefreshToken).where(
                            RefreshToken.tenant_id == tenant_id,
                            RefreshToken.token_hash == token_hash,
                        )
                    )
                ).scalar_one_or_none()
                if tok_row:
                    await session.execute(
                        update(RefreshToken)
                        .where(
                            RefreshToken.tenant_id == tenant_id,
                            RefreshToken.family_id == tok_row.family_id,
                        )
                        .values(revoked_at=datetime.now(UTC))
                    )
        except Exception:
            pass

    return {"message": "Successfully logged out."}


@router.get("/me", response_model=UserProfileResponse)
async def get_me(
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_current_session),
) -> UserProfileResponse:
    """Return profile of currently authenticated user within active tenant."""
    tenant = (
        await session.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    ).scalar_one()

    return UserProfileResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        full_name=current_user.full_name,
        tenant_id=current_user.tenant_id,
        role=current_user.role.value,
        tenant_name=tenant.name,
        tenant_slug=tenant.slug,
    )


@router.post("/accept-invite", response_model=TokenResponse)
async def accept_invitation(payload: AcceptInviteRequest) -> TokenResponse:
    """Accept invitation and activate tenant membership."""
    try:
        tenant_id, secret = parse_prefixed_token(payload.token)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid invite token: {e}",
        ) from e

    token_hash = hash_token_secret(payload.token)

    async with async_session_maker() as session:
        async with session.begin():
            await set_tenant_context(session, tenant_id)

            # Find invitation
            inv_stmt = select(Invitation).where(
                Invitation.tenant_id == tenant_id,
                Invitation.token_hash == token_hash,
            )
            invitation = (await session.execute(inv_stmt)).scalar_one_or_none()
            if not invitation:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid or expired invitation token.",
                )

            now = datetime.now(UTC)
            if invitation.accepted_at is not None or invitation.expires_at <= now:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invitation has expired or already been accepted.",
                )

            # Check if user already exists
            user_stmt = select(User).where(func.lower(User.email) == invitation.email.lower())
            user = (await session.execute(user_stmt)).scalar_one_or_none()

            if user:
                # User exists: verify password
                if not verify_password(payload.password, user.password_hash):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Existing user password does not match.",
                    )
            else:
                # Create user
                user_id = uuid.uuid4()
                user = User(
                    id=user_id,
                    email=invitation.email.lower(),
                    password_hash=hash_password(payload.password),
                    full_name=payload.full_name,
                    is_active=True,
                )
                session.add(user)
                await session.flush()

            # Create or update membership
            mem_stmt = select(Membership).where(
                Membership.tenant_id == tenant_id,
                Membership.user_id == user.id,
            )
            membership = (await session.execute(mem_stmt)).scalar_one_or_none()
            if membership:
                membership.is_active = True
                membership.role = invitation.role
            else:
                membership = Membership(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    user_id=user.id,
                    role=invitation.role,
                    is_active=True,
                )
                session.add(membership)

            # Mark invitation as accepted
            invitation.accepted_at = now

            # Generate tokens
            access_tok, jti, exp_ts = create_access_token(user.id, tenant_id)
            refresh_tok_str, refresh_hash = generate_opaque_token(tenant_id)

            refresh_token = RefreshToken(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user.id,
                family_id=uuid.uuid4(),
                token_hash=refresh_hash,
                expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            )
            session.add(refresh_token)

    return TokenResponse(
        access_token=access_tok,
        refresh_token=refresh_tok_str,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
