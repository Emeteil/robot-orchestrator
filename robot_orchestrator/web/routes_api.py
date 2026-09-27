import asyncio
import dataclasses
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from robot_orchestrator.secrets import store as secrets_store
from robot_orchestrator.secrets.ingest import ingest_payload
from robot_orchestrator.secrets.protocol import SecretsPayload
from robot_orchestrator.state import store
from robot_orchestrator.supervisor.service import ServiceState
from robot_orchestrator.web import auth
from robot_orchestrator.web.context import AdminContext


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def _require(context: AdminContext | None) -> AdminContext:
    if context is None:
        raise HTTPException(status_code=503, detail="orchestrator context not available")
    return context


def _require_service_name(context: AdminContext, name: str) -> None:
    if name not in context.supervisor.services:
        raise HTTPException(status_code=404, detail=f"unknown service {name!r}")


def _require_repo_manager(context: AdminContext, name: str):
    manager = context.repo_managers.get(name)
    if manager is None:
        raise HTTPException(status_code=404, detail=f"unknown repo {name!r}")
    return manager


def _require_firmware_workflow(context: AdminContext, name: str):
    workflow = context.firmware_workflows.get(name)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"unknown firmware target {name!r}")
    return workflow


def register_routes(app: FastAPI, context: AdminContext | None, require_auth) -> None:

    @app.get("/api/state")
    def get_state(claims: dict = Depends(require_auth)):
        c = _require(context)
        boot = c.boot_result
        if boot is None:
            return {"boot": None}
        return {
            "boot_id": boot.boot_id,
            "production": boot.mode.production,
            "reasons": boot.mode.reasons,
            "capabilities": boot.mode.capabilities,
            "facts": dataclasses.asdict(boot.facts),
            "services_ready": boot.services_ready,
        }

    @app.get("/api/services")
    def list_services(claims: dict = Depends(require_auth)):
        c = _require(context)
        services = []
        for name, service in c.supervisor.services.items():
            running = service.process is not None and service.process.returncode is None
            services.append({
                "name": name,
                "state": service.state.value,
                "pid": service.process.pid if running else None,
                "last_exit_code": service.last_exit_code,
                "holds_devices": service.config.holds_devices,
            })
        return services

    @app.post("/api/services/{name}/start")
    async def start_service(name: str, claims: dict = Depends(require_auth)):
        c = _require(context)
        _require_service_name(c, name)
        service = c.supervisor.services[name]
        if service.state in (ServiceState.STOPPED, ServiceState.FAILED, ServiceState.SKIPPED):
            started = await c.supervisor.restart(name)
            return {"started": started}
        return {"started": True}

    @app.post("/api/services/{name}/stop")
    async def stop_service(name: str, claims: dict = Depends(require_auth)):
        c = _require(context)
        _require_service_name(c, name)
        await c.supervisor.stop(name)
        return {"stopped": True}

    @app.post("/api/services/{name}/restart")
    async def restart_service(name: str, claims: dict = Depends(require_auth)):
        c = _require(context)
        _require_service_name(c, name)
        started = await c.supervisor.restart(name)
        return {"started": started}

    @app.get("/api/repos")
    def list_repos(claims: dict = Depends(require_auth)):
        c = _require(context)
        repos = []
        for repo_cfg in c.settings.repos:
            state = store.get_repo_state(c.db.conn, repo_cfg.name) or {}
            repos.append({"name": repo_cfg.name, "url": repo_cfg.url, "branch": repo_cfg.branch, **state})
        return repos

    @app.post("/api/repos/{name}/check")
    def check_repo(name: str, claims: dict = Depends(require_auth)):
        c = _require(context)
        manager = _require_repo_manager(c, name)
        result = manager.check()
        return {
            "pending": result.pending, "current_sha": result.current_sha,
            "remote_sha": result.remote_sha, "error": result.error,
        }

    @app.post("/api/repos/{name}/apply")
    def apply_repo(name: str, claims: dict = Depends(require_auth)):
        c = _require(context)
        manager = _require_repo_manager(c, name)
        result = manager.sync()
        return {"changed": result.changed, "sha": result.sha, "error": result.error}

    @app.post("/api/repos/{name}/rollback")
    def rollback_repo(name: str, claims: dict = Depends(require_auth)):
        c = _require(context)
        manager = _require_repo_manager(c, name)
        return {"rolled_back": manager.rollback()}

    @app.get("/api/firmware")
    def list_firmware(claims: dict = Depends(require_auth)):
        c = _require(context)
        targets = []
        for target in c.settings.firmware_targets:
            state = store.get_flash_state(c.db.conn, target.name) or {}
            targets.append({"name": target.name, "repo": target.repo, **state})
        return targets

    @app.post("/api/firmware/{name}/reverify")
    def reverify_firmware(name: str, claims: dict = Depends(require_auth)):
        c = _require(context)
        workflow = _require_firmware_workflow(c, name)
        outcome = workflow.verify_only()
        return {"state": outcome.state, "reason": outcome.reason}

    @app.post("/api/firmware/{name}/reflash")
    def reflash_firmware(name: str, claims: dict = Depends(require_auth)):
        c = _require(context)
        workflow = _require_firmware_workflow(c, name)
        repo_state = store.get_repo_state(c.db.conn, workflow.target.repo)
        if not repo_state or not repo_state.get("current_release"):
            raise HTTPException(status_code=409, detail="repo release not available yet")
        outcome = workflow.force_reflash(repo_state["current_sha"], Path(repo_state["current_release"]))
        return {"state": outcome.state, "reason": outcome.reason}

    @app.get("/api/logs/{service}")
    def get_logs(service: str, lines: int = 500, claims: dict = Depends(require_auth)):
        c = _require(context)
        sink = c.supervisor.log_sinks.get(service)
        if sink is None:
            raise HTTPException(status_code=404, detail=f"unknown service {service!r}")
        return {"lines": sink.tail(lines)}

    @app.get("/api/journal")
    def get_journal(limit: int = 50, claims: dict = Depends(require_auth)):
        c = _require(context)
        rows = c.db.conn.execute(
            "SELECT * FROM journal ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/api/secrets")
    def get_secrets(claims: dict = Depends(require_auth)):
        c = _require(context)
        generated = secrets_store.ensure_generated_secrets(c.paths)
        regular = secrets_store.load_secrets(c.paths).get("secrets", {}) or {}
        combined = {**generated, **regular}
        return {
            "required": [{"key": k, "present": bool(combined.get(k))} for k in c.settings.secrets.required],
            "optional": [{"key": k, "present": bool(combined.get(k))} for k in c.settings.secrets.optional],
            "is_default_password": auth.is_default_password(c.paths),
        }

    @app.post("/api/secrets/import")
    def import_secrets(payload: dict, claims: dict = Depends(require_auth)):
        c = _require(context)
        try:
            secrets_payload = SecretsPayload.model_validate(payload)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid secrets payload: {e}")
        ingest_payload(c.paths, c.journal, secrets_payload)
        return {"imported": True}

    @app.post("/api/secrets/rescan")
    async def rescan_secrets(claims: dict = Depends(require_auth)):
        c = _require(context)
        if c.scan_qr is None:
            raise HTTPException(status_code=503, detail="qr scanning is not available")
        payload = await asyncio.to_thread(c.scan_qr)
        if payload is None:
            return {"scanned": False}
        ingest_payload(c.paths, c.journal, payload)
        return {"scanned": True}

    @app.get("/api/wifi")
    def get_wifi(claims: dict = Depends(require_auth)):
        c = _require(context)
        known = secrets_store.load_wifi_credentials(c.paths)
        visible = c.wifi_manager.list_visible_ssids() if c.wifi_manager is not None else []
        return {"known_ssids": [w.ssid for w in known], "visible_ssids": visible}

    @app.post("/api/wifi/connect")
    async def connect_wifi(claims: dict = Depends(require_auth)):
        c = _require(context)
        if c.wifi_manager is None:
            raise HTTPException(status_code=503, detail="wifi manager is not available")
        known = secrets_store.load_wifi_credentials(c.paths)
        connected = await asyncio.to_thread(c.wifi_manager.try_known_networks, known)
        return {"connected_ssid": connected}

    @app.post("/api/settings/password")
    def change_password(body: ChangePasswordRequest, claims: dict = Depends(require_auth)):
        c = _require(context)
        if not auth.verify_login(c.paths, claims["sub"], body.current_password):
            raise HTTPException(status_code=401, detail="current password is incorrect")
        auth.save_admin(c.paths, claims["sub"], body.new_password)
        return {"changed": True}

    @app.post("/api/system/restart")
    def restart_system(claims: dict = Depends(require_auth)):
        c = _require(context)
        if c.request_restart is None:
            raise HTTPException(status_code=503, detail="restart is not available in this context")
        c.request_restart()
        return {"restart_requested": True}

    @app.get("/api/self-update")
    def get_self_update(claims: dict = Depends(require_auth)):
        c = _require(context)
        if c.self_update_manager is None:
            raise HTTPException(status_code=503, detail="self-update is not available in this context")
        status = c.self_update_manager.status()
        return {
            "enabled": c.settings.self_update.enabled,
            "current_sha": status.current_sha, "staged_sha": status.staged_sha,
            "staged_at": status.staged_at, "error": status.error,
        }

    @app.post("/api/self-update/check")
    async def check_self_update(claims: dict = Depends(require_auth)):
        c = _require(context)
        if c.self_update_manager is None:
            raise HTTPException(status_code=503, detail="self-update is not available in this context")
        status = await asyncio.to_thread(c.self_update_manager.check_and_stage)
        return {
            "current_sha": status.current_sha, "staged_sha": status.staged_sha,
            "staged_at": status.staged_at, "error": status.error,
        }

    @app.post("/api/self-update/apply")
    def apply_self_update(claims: dict = Depends(require_auth)):
        c = _require(context)
        if c.self_update_manager is None:
            raise HTTPException(status_code=503, detail="self-update is not available in this context")
        applied = c.self_update_manager.apply()
        if applied and c.request_restart is not None:
            c.request_restart()
        return {"applied": applied}
