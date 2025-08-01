from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, replace
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pdm._types import HiddenText
from pdm.models.candidates import Candidate
from pdm.models.markers import get_marker
from pdm.models.repositories import Package
from pdm.models.requirements import FileRequirement, NamedRequirement, Requirement, VcsRequirement
from pdm.models.specifiers import get_specifier
from pdm.project.lockfile import FLAG_DIRECT_MINIMAL_VERSIONS, FLAG_INHERIT_METADATA
from pdm.resolver.base import Resolution, Resolver
from pdm.resolver.reporters import RichLockReporter
from pdm.termui import Verbosity
from pdm.utils import normalize_name

if TYPE_CHECKING:
    from pdm._types import FileHash

logger = logging.getLogger(__name__)

GIT_URL = re.compile(r"(?P<repo>[^:/]+://[^\?#]+)(?:\?rev=(?P<ref>[^#]+?))?(?:#(?P<revision>[a-f0-9]+))$")


@dataclass
class UvResolver(Resolver):
    def __post_init__(self) -> None:
        super().__post_init__()

        if self.locked_repository is None:
            self.locked_repository = self.project.get_locked_repository()
        if self.update_strategy not in {"reuse", "all"}:
            self.project.core.ui.warn(
                f"{self.update_strategy} update strategy is not supported by uv, using 'reuse' instead"
            )
            self.update_strategy = "reuse"
        if FLAG_INHERIT_METADATA in self.strategies:
            self.project.core.ui.warn("inherit_metadata strategy is not supported by uv resolver, it will be ignored")
            self.strategies.discard(FLAG_INHERIT_METADATA)
        this_spec = self.environment.spec
        assert this_spec.platform is not None
        if self.target.platform and (
            self.target.platform.sys_platform != this_spec.platform.sys_platform
            or self.target.platform.arch != this_spec.platform.arch
        ):
            self.project.core.ui.warn(
                f"Resolving against target {self.target.platform} on {this_spec.platform} is not supported by uv mode, "
                "the resolution may be inaccurate."
            )

    def _build_lock_command(self) -> list[str | HiddenText]:
        cmd: list[str | HiddenText] = [
            *self.project.core.uv_cmd,
            "lock",
            "-p",
            str(self.environment.interpreter.executable),
        ]
        if self.project.core.ui.verbosity > 0:
            cmd.append("--verbose")
        if not self.project.core.state.enable_cache:
            cmd.append("--no-cache")
        first_index = True
        for source in self.project.sources:
            url = source.url_with_credentials
            if source.type == "find_links":
                cmd.extend(["--find-links", url])
            elif first_index:
                cmd.extend(["--index-url", url])
                first_index = False
            else:
                cmd.extend(["--extra-index-url", url])
        if self.project.pyproject.settings.get("resolution", {}).get("respect-source-order", False):
            cmd.append("--index-strategy=unsafe-first-match")
        else:
            cmd.append("--index-strategy=unsafe-best-match")
        if self.update_strategy != "all":
            for name in self.tracked_names:
                cmd.extend(["-P", name])
        if self.project.pyproject.allow_prereleases:
            cmd.append("--prerelease=allow")
        no_binary = self.environment._setting_list("PDM_NO_BINARY", "resolution.no-binary")
        only_binary = self.environment._setting_list("PDM_ONLY_BINARY", "resolution.only-binary")
        if ":all:" in no_binary:
            cmd.append("--no-binary")
        else:
            for pkg in no_binary:
                cmd.extend(["--no-binary-package", pkg])
        if ":all:" in only_binary:
            cmd.append("--no-build")
        else:
            for pkg in only_binary:
                cmd.extend(["--no-build-package", pkg])
        if not self.project.core.state.build_isolation:
            cmd.append("--no-build-isolation")
        if cs := self.project.core.state.config_settings:
            for k, v in cs.items():
                cmd.extend(["--config-setting", f"{k}={v}"])

        if FLAG_DIRECT_MINIMAL_VERSIONS in self.strategies:
            cmd.append("--resolution=lowest-direct")

        if dt := self.project.core.state.exclude_newer:
            cmd.extend(["--exclude-newer", dt.isoformat()])

        return cmd

    def _parse_uv_lock(self, path: Path) -> Resolution:
        from unearth import Link

        from pdm.compat import tomllib

        with path.open("rb") as f:
            data = tomllib.load(f)

        packages: list[Package] = []
        hash_cache = self.project.make_hash_cache()
        session = self.environment.session

        def make_requirement(dep: dict[str, Any]) -> str:
            req = NamedRequirement(name=dep["name"])
            if version := dep.get("version"):
                req.specifier = get_specifier(f"=={version}")
            if marker := dep.get("marker"):
                req.marker = get_marker(marker)
            if extra := dep.get("extra"):
                req.extras = extra
            return req.as_line()

        def make_hash(item: dict[str, Any]) -> FileHash:
            link = Link(item["url"])
            if "hash" not in item:
                item["hash"] = hash_cache.get_hash(link, session)
            return {"url": item["url"], "file": link.filename, "hash": item["hash"]}

        for package in data["package"]:
            if (
                self.project.name
                and package["name"] == normalize_name(self.project.name)
                and (not self.keep_self or package["source"].get("virtual"))
            ):
                continue
            req: Requirement
            if url := package["source"].get("url"):
                req = FileRequirement.create(url=url, name=package["name"])
            elif git := package["source"].get("git"):
                matches = GIT_URL.match(git)
                if not matches:
                    raise ValueError(f"Invalid git URL: {git}")
                url = f"git+{matches.group('repo')}"
                if ref := matches.group("ref"):
                    url += f"@{ref}"
                req = VcsRequirement.create(url=url, name=package["name"])
                req.revision = matches.group("revision")
            elif editable := package["source"].get("editable"):
                req = FileRequirement.create(path=editable, name=package["name"], editable=True)
            elif filepath := package["source"].get("path"):
                req = FileRequirement.create(path=filepath, name=package["name"])
            else:
                req = NamedRequirement.create(name=package["name"], specifier=f"=={package['version']}")
            candidate = Candidate(req, name=package["name"], version=package["version"])

            for wheel in chain(package.get("wheels", []), [sdist] if (sdist := package.get("sdist")) else []):
                candidate.hashes.append(make_hash(wheel))
            entry = Package(candidate, [make_requirement(dep) for dep in package.get("dependencies", [])], "")
            packages.append(entry)
            if optional_dependencies := package.get("optional-dependencies"):
                for group, deps in optional_dependencies.items():
                    extra_entry = Package(
                        candidate.copy_with(replace(req, extras=(group,))),
                        [f"{req.key}=={candidate.version}", *(make_requirement(dep) for dep in deps)],
                        "",
                    )
                    packages.append(extra_entry)
        return Resolution(packages, self.requested_groups)

    def resolve(self) -> Resolution:
        from pdm.formats.uv import uv_file_builder

        locked_repo = self.locked_repository or self.project.get_locked_repository()
        with uv_file_builder(self.project, str(self.target.requires_python), self.requirements, locked_repo) as builder:
            builder.build_pyproject_toml()
            uv_lock_path = self.project.root / "uv.lock"
            if self.update_strategy != "all":
                builder.build_uv_lock()
            try:
                if isinstance(self.reporter, RichLockReporter):
                    self.reporter.stop()
                uv_lock_command = self._build_lock_command()
                self.project.core.ui.echo(f"Running uv lock command: {uv_lock_command}", verbosity=Verbosity.DETAIL)
                real_command = [s.secret if isinstance(s, HiddenText) else s for s in uv_lock_command]
                subprocess.run(real_command, cwd=self.project.root, check=True)
            finally:
                if isinstance(self.reporter, RichLockReporter):
                    self.reporter.start()
            return self._parse_uv_lock(uv_lock_path)
