#!/usr/bin/env python3
"""Synchronize trusted lifecycle preflight artifacts for quant-monitor."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence


DOMAIN_CONFIGS: dict[str, dict[str, str]] = {
    "cn_equity": {
        "domain": "cn_equity",
        "repository": "QuantStrategyLab/CnEquityStrategies",
        "snapshot_repository": "CnEquitySnapshotPipelines",
        "artifact_prefix": "lifecycle-preflight-",
        "workflow_path": ".github/workflows/drift-check.yml",
        "preflight_job": "preflight_backtests",
        "benchmark_column": "buy_hold_510300",
    },
    "hk_equity": {
        "domain": "hk_equity",
        "repository": "QuantStrategyLab/HkEquityStrategies",
        "snapshot_repository": "HkEquitySnapshotPipelines",
        "artifact_prefix": "lifecycle-preflight-",
        "workflow_path": ".github/workflows/drift-check.yml",
        "preflight_job": "preflight_backtests",
        "benchmark_column": "buy_hold_2800",
    },
    "us_equity": {
        "domain": "us_equity",
        "repository": "QuantStrategyLab/UsEquityStrategies",
        "snapshot_repository": "UsEquitySnapshotPipelines",
        "artifact_prefix": "lifecycle-preflight-",
        "workflow_path": ".github/workflows/drift-check.yml",
        "preflight_job": "preflight_backtests",
        "benchmark_column": "buy_hold_SPY",
    },
    "crypto": {
        "domain": "crypto",
        "repository": "QuantStrategyLab/CryptoStrategies",
        "snapshot_repository": "CryptoLivePoolPipelines",
        "artifact_prefix": "lifecycle-preflight-",
        "workflow_path": ".github/workflows/drift-check.yml",
        "preflight_job": "preflight_backtests",
        "benchmark_column": "buy_hold_BTC",
    },
}

_TRUSTED_EVENTS = frozenset({"schedule", "workflow_dispatch"})
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_MAX_FILES = 256
_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_TOTAL_BYTES = 256 * 1024 * 1024
_MANIFEST_NAME = ".artifact-manifest.json"


class LifecycleArtifactError(RuntimeError):
    """A fail-closed lifecycle artifact synchronization error."""

    def __init__(self, message: str, *, code: str = "artifact_invalid") -> None:
        super().__init__(message)
        self.code = code


def _parse_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LifecycleArtifactError("artifact timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise LifecycleArtifactError("artifact timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _trusted_run(
    config: Mapping[str, str],
    run: Mapping[str, Any],
    jobs_payload: Mapping[str, Any],
) -> bool:
    repository = run.get("head_repository")
    jobs = jobs_payload.get("jobs")
    if not isinstance(repository, Mapping) or not isinstance(jobs, list):
        return False
    if (
        run.get("status") != "completed"
        or run.get("event") not in _TRUSTED_EVENTS
        or run.get("head_branch") != "main"
        or run.get("path") != config["workflow_path"]
        or repository.get("full_name") != config["repository"]
        or not _SHA_PATTERN.fullmatch(str(run.get("head_sha") or ""))
    ):
        return False
    return any(
        isinstance(job, Mapping)
        and job.get("name") == config["preflight_job"]
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
        for job in jobs
    )


def select_trusted_artifact(
    config: Mapping[str, str],
    artifacts: Sequence[Mapping[str, Any]],
    *,
    load_run: Callable[[int], Mapping[str, Any]],
    load_jobs: Callable[[int], Mapping[str, Any]],
    now: datetime,
    max_age: timedelta,
) -> dict[str, Any]:
    """Return the newest artifact whose workflow provenance is trusted."""

    now = now.astimezone(timezone.utc)
    prefix = config["artifact_prefix"]
    candidates: list[tuple[datetime, Mapping[str, Any]]] = []
    for artifact in artifacts:
        try:
            created_at = _parse_timestamp(artifact.get("created_at"))
        except LifecycleArtifactError:
            continue
        if (
            artifact.get("expired") is True
            or not str(artifact.get("name") or "").startswith(prefix)
            or created_at > now + timedelta(minutes=5)
            or now - created_at > max_age
        ):
            continue
        candidates.append((created_at, artifact))

    for created_at, artifact in sorted(candidates, key=lambda item: item[0], reverse=True):
        workflow_run = artifact.get("workflow_run")
        if not isinstance(workflow_run, Mapping):
            continue
        try:
            run_id = int(workflow_run.get("id"))
            artifact_id = int(artifact.get("id"))
        except (TypeError, ValueError):
            continue
        expected_name = re.fullmatch(
            rf"{re.escape(prefix)}{run_id}-[1-9][0-9]*",
            str(artifact.get("name") or ""),
        )
        if expected_name is None:
            continue
        run = load_run(run_id)
        if int(run.get("id") or 0) != run_id:
            continue
        jobs = load_jobs(run_id)
        if not _trusted_run(config, run, jobs):
            continue
        return {
            **artifact,
            "id": artifact_id,
            "_trusted_run": dict(run),
            "_created_at": created_at.isoformat(),
        }

    raise LifecycleArtifactError(
        f"no trusted lifecycle artifact for {config['domain']}",
        code="trusted_artifact_unavailable",
    )


def _safe_member_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise LifecycleArtifactError("archive contains an unsafe path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise LifecycleArtifactError("archive contains an unsafe path")
    return path


def _classify_member(
    path: PurePosixPath,
    config: Mapping[str, str],
) -> tuple[str, str] | None:
    parts = path.parts
    domain = config["domain"]
    snapshot_repository = config["snapshot_repository"]
    if (
        len(parts) == 6
        and parts[:4] == ("data", "lifecycle_store", "backtest", domain)
        and _PROFILE_PATTERN.fullmatch(parts[4])
        and parts[5].startswith("backtest_")
        and parts[5].endswith(".json")
    ):
        return "backtest", parts[4]
    if (
        len(parts) == 6
        and parts[:4] == ("external", snapshot_repository, "data", "output")
        and _PROFILE_PATTERN.fullmatch(parts[4])
        and parts[5] == "portfolio_and_tracker_returns.csv"
    ):
        return "matrix", parts[4]
    return None


def _validate_backtest(
    raw: bytes,
    *,
    domain: str,
    profile: str,
) -> None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleArtifactError("backtest JSON is invalid") from exc
    if not isinstance(payload, Mapping):
        raise LifecycleArtifactError("backtest JSON must contain an object")
    if payload.get("domain") != domain or payload.get("strategy_profile") != profile:
        raise LifecycleArtifactError("backtest domain or profile does not match its path")
    if (
        payload.get("schema_version") != "strategy_lifecycle.v1"
        or not str(payload.get("param_set_id") or "").strip()
        or not isinstance(payload.get("params"), Mapping)
        or isinstance(payload.get("param_version"), bool)
        or not isinstance(payload.get("param_version"), int)
        or payload["param_version"] < 0
        or isinstance(payload.get("observation_count"), bool)
        or not isinstance(payload.get("observation_count"), int)
        or payload["observation_count"] < 2
        or not str(payload.get("source_script") or "").strip()
    ):
        raise LifecycleArtifactError("backtest JSON lifecycle contract is invalid")
    for key in ("sharpe_ratio", "max_drawdown", "cagr", "volatility"):
        try:
            value = float(payload[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise LifecycleArtifactError(
                "backtest JSON lifecycle metrics are invalid"
            ) from exc
        if not math.isfinite(value):
            raise LifecycleArtifactError("backtest JSON lifecycle metrics are invalid")
    try:
        start_date = date.fromisoformat(str(payload["start_date"]))
        end_date = date.fromisoformat(str(payload["end_date"]))
        _parse_timestamp(payload["computed_at"])
    except (KeyError, ValueError) as exc:
        raise LifecycleArtifactError("backtest JSON lifecycle dates are invalid") from exc
    if end_date < start_date:
        raise LifecycleArtifactError("backtest JSON lifecycle dates are invalid")


def _validate_matrix(raw: bytes, *, profile: str, benchmark_column: str) -> None:
    try:
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise LifecycleArtifactError("return matrix CSV is invalid") from exc
    if len(rows) < 3:
        raise LifecycleArtifactError("return matrix must contain at least two data rows")
    header = [column.strip() for column in rows[0]]
    if (
        len(header) != 3
        or header[0] != "as_of"
        or header[1] != profile
        or header[2] != benchmark_column
    ):
        raise LifecycleArtifactError("return matrix profile or header is invalid")
    previous_date = ""
    observation_counts = [0, 0]
    for row in rows[1:]:
        if len(row) != len(header):
            raise LifecycleArtifactError("return matrix row width is invalid")
        date_value = row[0].strip()
        try:
            datetime.strptime(date_value, "%Y-%m-%d")
        except (ValueError, TypeError) as exc:
            raise LifecycleArtifactError("return matrix contains invalid values") from exc
        for index, raw_value in enumerate(row[1:]):
            if not raw_value.strip():
                continue
            try:
                value = float(raw_value)
            except ValueError as exc:
                raise LifecycleArtifactError(
                    "return matrix contains invalid values"
                ) from exc
            if not math.isfinite(value):
                raise LifecycleArtifactError("return matrix contains non-finite values")
            observation_counts[index] += 1
        if previous_date and date_value <= previous_date:
            raise LifecycleArtifactError("return matrix dates must be strictly increasing")
        previous_date = date_value
    if any(count < 2 for count in observation_counts):
        raise LifecycleArtifactError("return matrix has insufficient finite observations")


def extract_validated_archive(
    archive_path: Path,
    output_dir: Path,
    config: Mapping[str, str],
) -> dict[str, Any]:
    """Validate a lifecycle artifact completely before writing allowlisted files."""

    files: dict[PurePosixPath, bytes] = {}
    total_size = 0
    backtest_profiles: set[str] = set()
    matrix_profiles: set[str] = set()
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise LifecycleArtifactError("artifact archive is invalid") from exc

    with archive:
        members = archive.infolist()
        if len(members) > _MAX_FILES:
            raise LifecycleArtifactError("artifact archive contains too many entries")
        for member in members:
            path = _safe_member_path(member.filename)
            mode = (member.external_attr >> 16) & 0xFFFF
            if member.is_dir():
                continue
            file_type = stat.S_IFMT(mode)
            if file_type and not stat.S_ISREG(mode):
                raise LifecycleArtifactError("artifact archive contains a non-regular file")
            classification = _classify_member(path, config)
            if classification is None:
                raise LifecycleArtifactError("artifact archive contains an unexpected file")
            if path in files:
                raise LifecycleArtifactError("artifact archive contains a duplicate path")
            if member.file_size > _MAX_FILE_BYTES:
                raise LifecycleArtifactError("artifact archive file exceeds the size limit")
            total_size += member.file_size
            if total_size > _MAX_TOTAL_BYTES:
                raise LifecycleArtifactError("artifact archive exceeds the total size limit")
            try:
                raw = archive.read(member)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise LifecycleArtifactError("artifact archive could not be read") from exc
            if len(raw) != member.file_size:
                raise LifecycleArtifactError("artifact archive entry size is inconsistent")
            kind, profile = classification
            if kind == "backtest":
                _validate_backtest(raw, domain=config["domain"], profile=profile)
                backtest_profiles.add(profile)
            else:
                _validate_matrix(
                    raw,
                    profile=profile,
                    benchmark_column=config["benchmark_column"],
                )
                matrix_profiles.add(profile)
            files[path] = raw

    if not files or not backtest_profiles or backtest_profiles != matrix_profiles:
        raise LifecycleArtifactError("backtest and return matrix profiles do not match")
    if output_dir.exists() or os.path.lexists(output_dir):
        raise LifecycleArtifactError("artifact output directory already exists")
    output_dir.mkdir(parents=True)
    for path, raw in files.items():
        destination = output_dir.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    return {
        "profiles": sorted(backtest_profiles),
        "file_count": len(files),
        "total_bytes": total_size,
        "sha256": {
            path.as_posix(): hashlib.sha256(raw).hexdigest()
            for path, raw in sorted(files.items(), key=lambda item: item[0].as_posix())
        },
    }


def validate_stored_version(
    version_dir: Path,
    manifest: Mapping[str, Any],
    config: Mapping[str, str],
) -> None:
    """Revalidate cached files and hashes before each activation."""

    hashes = manifest.get("sha256")
    profiles = manifest.get("profiles")
    if not isinstance(hashes, Mapping) or not hashes or not isinstance(profiles, list):
        raise LifecycleArtifactError("stored artifact manifest is incomplete")
    expected_files: set[str] = set()
    backtest_profiles: set[str] = set()
    matrix_profiles: set[str] = set()
    for raw_name, raw_digest in hashes.items():
        path = _safe_member_path(str(raw_name))
        classification = _classify_member(path, config)
        digest = str(raw_digest or "")
        if classification is None or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise LifecycleArtifactError("stored artifact manifest contains invalid files")
        destination = version_dir.joinpath(*path.parts)
        managed_parent = version_dir
        has_symlink_parent = version_dir.is_symlink()
        for part in path.parts[:-1]:
            managed_parent /= part
            has_symlink_parent = has_symlink_parent or managed_parent.is_symlink()
        if destination.is_symlink() or has_symlink_parent:
            raise LifecycleArtifactError("stored artifact contains a symlink")
        if not destination.is_file():
            raise LifecycleArtifactError("stored artifact file is missing")
        raw = destination.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise LifecycleArtifactError("stored artifact file hash does not match")
        kind, profile = classification
        if kind == "backtest":
            _validate_backtest(raw, domain=config["domain"], profile=profile)
            backtest_profiles.add(profile)
        else:
            _validate_matrix(
                raw,
                profile=profile,
                benchmark_column=config["benchmark_column"],
            )
            matrix_profiles.add(profile)
        expected_files.add(path.as_posix())

    actual_files: set[str] = set()
    for path in version_dir.rglob("*"):
        if path.is_symlink():
            raise LifecycleArtifactError("stored artifact contains a symlink")
        if path.is_file():
            relative = path.relative_to(version_dir).as_posix()
            if relative != _MANIFEST_NAME:
                actual_files.add(relative)
    if (
        actual_files != expected_files
        or backtest_profiles != matrix_profiles
        or sorted(backtest_profiles) != sorted(profiles)
    ):
        raise LifecycleArtifactError("stored artifact manifest does not match its files")


def _replace_managed_symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(link) and not link.is_symlink():
        raise LifecycleArtifactError(
            f"refusing to replace unmanaged path: {link}",
            code="consumer_path_conflict",
        )
    temporary = link.parent / f".{link.name}.tmp-{os.getpid()}"
    try:
        if os.path.lexists(temporary):
            temporary.unlink()
        temporary.symlink_to(target)
        temporary.replace(link)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def activate_version(
    version_dir: Path,
    config: Mapping[str, str],
    *,
    projects_root: Path,
    lifecycle_root: Path,
) -> None:
    """Atomically activate a validated version for both lifecycle consumers."""

    version_dir = version_dir.absolute()
    domain = config["domain"]
    if (
        not version_dir.is_dir()
        or version_dir.parent.name != domain
        or version_dir.parent.parent.name != "versions"
    ):
        raise LifecycleArtifactError("artifact version path is invalid")
    artifacts_root = version_dir.parents[2]
    current_link = artifacts_root / "current" / domain
    _replace_managed_symlink(current_link, version_dir)

    matrix_target = (
        current_link
        / "external"
        / config["snapshot_repository"]
        / "data"
        / "output"
    )
    backtest_target = (
        current_link
        / "data"
        / "lifecycle_store"
        / "backtest"
        / domain
    )
    if not matrix_target.is_dir() or not backtest_target.is_dir():
        raise LifecycleArtifactError("activated artifact is missing required directories")
    _replace_managed_symlink(
        projects_root / config["snapshot_repository"] / "data" / "output",
        matrix_target,
    )
    _replace_managed_symlink(
        lifecycle_root / "backtest" / domain,
        backtest_target,
    )


def _run_gh(args: Sequence[str], *, binary: bool = False) -> Any:
    env = {**os.environ, "GH_PROMPT": "disabled"}
    for attempt in range(3):
        result = subprocess.run(
            ["gh", "api", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
            text=not binary,
        )
        if result.returncode == 0:
            if binary:
                return result.stdout
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise LifecycleArtifactError(
                    "GitHub API returned invalid JSON",
                    code="github_api_invalid",
                ) from exc
        if attempt < 2:
            time.sleep(2**attempt)
    raise LifecycleArtifactError(
        "GitHub API request failed",
        code="github_api_unavailable",
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
        temp_file.write("\n")
        temp_file.flush()
        os.fsync(temp_file.fileno())
    try:
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _load_or_download_version(
    artifact: Mapping[str, Any],
    config: Mapping[str, str],
    *,
    artifacts_root: Path,
) -> tuple[Path, Mapping[str, Any]]:
    artifact_id = int(artifact["id"])
    version_dir = artifacts_root / "versions" / config["domain"] / str(artifact_id)
    manifest_path = version_dir / _MANIFEST_NAME
    if manifest_path.is_file():
        if manifest_path.is_symlink():
            raise LifecycleArtifactError("stored artifact manifest must not be a symlink")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LifecycleArtifactError("stored artifact manifest is invalid") from exc
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("artifact_id") != artifact_id
            or manifest.get("domain") != config["domain"]
            or manifest.get("repository") != config["repository"]
            or manifest.get("artifact_name") != artifact["name"]
            or manifest.get("run_id") != int(artifact["_trusted_run"]["id"])
            or manifest.get("head_sha") != artifact["_trusted_run"]["head_sha"]
            or not isinstance(manifest.get("profiles"), list)
        ):
            raise LifecycleArtifactError("stored artifact manifest does not match")
        validate_stored_version(version_dir, manifest, config)
        return version_dir, manifest
    if os.path.lexists(version_dir):
        raise LifecycleArtifactError("stored artifact version is missing its manifest")

    version_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = version_dir.parent / f".{artifact_id}.staging-{os.getpid()}"
    if os.path.lexists(staging):
        shutil.rmtree(staging)
    try:
        archive_bytes = _run_gh(
            [
                f"/repos/{config['repository']}/actions/artifacts/{artifact_id}/zip",
                "-H",
                "Accept: application/vnd.github+json",
            ],
            binary=True,
        )
        with tempfile.NamedTemporaryFile(
            dir=version_dir.parent,
            prefix=f".{artifact_id}.",
            suffix=".zip",
            delete=False,
        ) as archive_file:
            archive_path = Path(archive_file.name)
            archive_file.write(archive_bytes)
        try:
            validation = extract_validated_archive(archive_path, staging, config)
        finally:
            archive_path.unlink(missing_ok=True)
        run = artifact["_trusted_run"]
        manifest = {
            "schema_version": "quant_monitor_lifecycle_artifact.v1",
            "domain": config["domain"],
            "repository": config["repository"],
            "artifact_id": artifact_id,
            "artifact_name": artifact["name"],
            "run_id": int(run["id"]),
            "head_sha": run["head_sha"],
            "created_at": artifact["_created_at"],
            **validation,
        }
        _atomic_write_json(staging / _MANIFEST_NAME, manifest)
        validate_stored_version(staging, manifest, config)
        try:
            staging.replace(version_dir)
        except FileExistsError:
            shutil.rmtree(staging)
            existing_manifest_path = version_dir / _MANIFEST_NAME
            if existing_manifest_path.is_symlink() or not existing_manifest_path.is_file():
                raise LifecycleArtifactError("stored artifact version is invalid")
            existing_manifest = json.loads(
                existing_manifest_path.read_text(encoding="utf-8")
            )
            if not isinstance(existing_manifest, Mapping):
                raise LifecycleArtifactError("stored artifact manifest is invalid")
            validate_stored_version(version_dir, existing_manifest, config)
            return version_dir, existing_manifest
        return version_dir, manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _sync_domain(
    config: Mapping[str, str],
    *,
    artifacts_root: Path,
    projects_root: Path,
    lifecycle_root: Path,
    max_age: timedelta,
    now: datetime,
) -> dict[str, Any]:
    repository = config["repository"]
    payload = _run_gh(
        [
            f"/repos/{repository}/actions/artifacts?per_page=100",
            "-H",
            "Accept: application/vnd.github+json",
        ]
    )
    artifacts = payload.get("artifacts") if isinstance(payload, Mapping) else None
    if not isinstance(artifacts, list):
        raise LifecycleArtifactError(
            "GitHub artifact listing is invalid",
            code="github_api_invalid",
        )
    selected = select_trusted_artifact(
        config,
        artifacts,
        load_run=lambda run_id: _run_gh(
            [f"/repos/{repository}/actions/runs/{run_id}"]
        ),
        load_jobs=lambda run_id: _run_gh(
            [f"/repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"]
        ),
        now=now,
        max_age=max_age,
    )
    version_dir, manifest = _load_or_download_version(
        selected,
        config,
        artifacts_root=artifacts_root,
    )
    activate_version(
        version_dir,
        config,
        projects_root=projects_root,
        lifecycle_root=lifecycle_root,
    )
    created_at = _parse_timestamp(selected["created_at"])
    return {
        "status": "ready",
        "artifact_id": int(selected["id"]),
        "run_id": int(selected["_trusted_run"]["id"]),
        "head_sha": selected["_trusted_run"]["head_sha"],
        "created_at": created_at.isoformat(),
        "age_seconds": max(0, int((now - created_at).total_seconds())),
        "profiles": list(manifest["profiles"]),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", action="append", choices=tuple(DOMAIN_CONFIGS))
    args = parser.parse_args(argv)

    root = Path(
        os.environ.get("QUANT_MONITOR_ROOT")
        or Path(__file__).resolve().parents[1]
    )
    artifacts_root = root / "data" / "lifecycle-artifacts"
    projects_root = Path(
        os.environ.get("QUANT_PROJECTS_ROOT")
        or root / "data" / "lifecycle-projects"
    )
    lifecycle_root = Path(
        os.environ.get("LIFECYCLE_LOCAL_ROOT")
        or root / "data" / "lifecycle-store"
    )
    try:
        max_age_hours = int(
            os.environ.get("QUANT_LIFECYCLE_ARTIFACT_MAX_AGE_HOURS") or "168"
        )
    except ValueError:
        max_age_hours = 168
    max_age = timedelta(hours=max(1, max_age_hours))
    now = datetime.now(timezone.utc)
    domains = tuple(args.domain or DOMAIN_CONFIGS)
    statuses: dict[str, dict[str, Any]] = {}
    for domain in domains:
        try:
            statuses[domain] = _sync_domain(
                DOMAIN_CONFIGS[domain],
                artifacts_root=artifacts_root,
                projects_root=projects_root,
                lifecycle_root=lifecycle_root,
                max_age=max_age,
                now=now,
            )
        except LifecycleArtifactError as exc:
            statuses[domain] = {
                "status": "error",
                "code": exc.code,
                "error_type": type(exc).__name__,
            }
        except Exception as exc:
            statuses[domain] = {
                "status": "error",
                "code": "artifact_sync_unexpected",
                "error_type": type(exc).__name__,
            }
    summary = {
        "schema_version": "quant_monitor_lifecycle_artifact_status.v1",
        "as_of": now.isoformat(),
        "domains": statuses,
        "ok": all(status.get("status") == "ready" for status in statuses.values()),
    }
    _atomic_write_json(artifacts_root / "status.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
