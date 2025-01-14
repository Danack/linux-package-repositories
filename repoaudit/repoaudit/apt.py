from collections import defaultdict
import re
import zlib
from typing import Optional, Set

import click
from debian.deb822 import Packages, Release
from requests.exceptions import HTTPError
import gnupg

from .utils import (RepoErrors, check_repo_empty, check_signature,
                    get_url, urljoin, package_output, verify_checksum)


CHECKSUMS = {
    "MD5sum": "md5",
    "MD5Sum": "md5",
    "SHA1": "sha1",
    "SHA256": "sha256",
    "SHA512": "sha512",
}
CHUNK_SIZE = 2 * 1024 * 1024


def _find_dists(url: str, verify: Optional[str] = None) -> Set[str]:
    """Find apt distributions."""
    try:
        resp = get_url(urljoin(url, "dists"), verify=verify)
    except HTTPError:
        raise

    links = re.findall(r"href=[\"'](.*)[\"']", resp.text)
    return {dist.strip("/") for dist in links if ".." not in dist}


def _packages_file(base_url: str, verify: Optional[str] = None) -> str:
    """Retrieve the packages file. It is possible for it to be compressed."""
    try:
        resp = get_url(urljoin(base_url, "Packages"), verify=verify)
        return resp.text
    except HTTPError as e:
        if e.response.status_code == 404:
            resp = get_url(urljoin(base_url, "Packages.gz"), verify=verify)
            return zlib.decompress(resp.content, 16 + zlib.MAX_WBITS).decode()
        else:
            raise e


def _check_apt_repo_metadata(url: str, dist: str, release_file: Release,
                             errors: RepoErrors, verify: Optional[str] = None) -> None:
    """Check repo metadata for checksum mismatches."""

    dist_url = urljoin(url, "dists", dist)
    checksums = set(CHECKSUMS.keys()) & set(release_file)

    success = True

    files = defaultdict(list)
    for key in checksums:
        for file_def in release_file[key]:
            filename = file_def['name']
            files[filename].append((CHECKSUMS[key], file_def[key.lower()]))

    if not files:
        errors.add(
            url, dist,
            urljoin(dist_url, "Release") + " file malformed"
        )
        success = False

    for file_name, expected_checksums in files.items():
        file_loc = urljoin("dists", dist, file_name)
        error_if_missing = f"{file_name}.gz" not in files

        success &= verify_checksum(
            url,
            dist,
            file_loc,
            "metadata",
            expected_checksums,
            errors,
            error_if_missing=error_if_missing,
            verify=verify
        )

    if success:
        click.echo("Metadata check successful")
    else:
        click.echo("Metadata check failed")


def _check_apt_signatures(url: str, dist: str, gpg: Optional[gnupg.GPG],
                          errors: RepoErrors, verify: Optional[str] = None) -> None:
    """Verify signature using provided public keys in gpg parameter."""

    if gpg is None:
        return

    dist_url = urljoin(url, "dists", dist)
    release_url = urljoin(dist_url, "Release")
    releasesig_url = urljoin(dist_url, "Release.gpg")
    inrelease_url = urljoin(dist_url, "InRelease")

    success = (
        check_signature(url, dist, release_url, gpg, errors,
                        signature_url=releasesig_url, verify=verify) &
        check_signature(url, dist, inrelease_url, gpg,
                        errors, verify=verify)
    )

    if success:
        click.echo("Signature check successful")
    else:
        click.echo("Signature check failed")


def _check_apt_packages(repo: str, dist: str, comp: str, arch: str, packages: str,
                        errors: RepoErrors, verify: Optional[str] = None) -> None:
    """Verifies the checksums for apt packages"""
    proc_package = 0

    try:
        # count the paragraphs
        package_count = len(re.findall(r"^Package: ", packages, re.MULTILINE))
        click.echo(f"Checking {dist}/{comp}/{arch}. Found {package_count} package(s).")

        dist_url = urljoin(repo, "dists", dist)
        with click.progressbar(
            Packages.iter_paragraphs(packages, use_apt_pkg=False),
            label="Checking packages(s)",
            length=package_count,
        ) as bar:
            for package in bar:
                if "Filename" not in package:
                    file_url = urljoin(dist_url, comp, f"binary-{arch}", "Packages")
                    errors.add(
                        repo, dist,
                        f"{file_url} file has a malformed package entry"
                        f"for package #{proc_package}"
                    )
                    continue

                checksums = set(CHECKSUMS.keys()) & set(package.keys())
                expected_checksums = [(CHECKSUMS[key], package[key]) for key in checksums]

                verify_checksum(
                    repo,
                    dist,
                    package["Filename"],
                    "package",
                    expected_checksums,
                    errors,
                    verify=verify
                )

                proc_package += 1
    except KeyboardInterrupt:
        package_output(proc_package)
        raise

    package_output(proc_package)


def check_apt_repo(url: str, dists: Optional[Set[str]], gpg: Optional[gnupg.GPG],
                   errors: RepoErrors, verify: Optional[str] = None) -> None:
    """Validate an apt repo."""

    click.echo(f"Validating apt repo at {url}...")
    errors.add(url, None, None)  # add empty entry with no errors

    if check_repo_empty(url, verify=verify):
        click.echo("Repository empty")
        return

    if not dists:
        try:
            dists = _find_dists(url, verify=verify)
        except HTTPError as e:
            error_str = f"Could not determine dists from {url}: {e}"
            errors.add(
                url, RepoErrors.APT_DIST,
                error_str
            )
            click.echo(error_str)
            return

    click.echo(f"Checking dists: {', '.join(dists)}")

    for dist in dists:
        try:
            errors.add(url, dist, None)  # add entry with no errors (yet)

            dist_url = urljoin(url, "dists", dist)
            release_url = urljoin(dist_url, "Release")
            try:
                release = get_url(release_url, verify=verify).text
            except HTTPError as e:
                errors.add(
                    url, dist,
                    f"Could not access Release file at {e.response.url}: {e}"
                )
                continue

            _check_apt_signatures(url, dist, gpg, errors, verify=verify)

            try:
                release_file = Release(release)
            except Exception:
                errors.add(
                    url, dist,
                    f"{release_url} file malformed"
                )
                continue

            _check_apt_repo_metadata(url, dist, release_file, errors, verify=verify)

            if "Components" not in release_file or "Architectures" not in release_file:
                errors.add(
                    url, dist,
                    f"{release_url} file malformed"
                )
                continue

            components = release_file["Components"].split()
            architectures = release_file["Architectures"].split()

            for comp in components:
                for arch in architectures:
                    try:
                        packages = _packages_file(urljoin(dist_url, comp, f"binary-{arch}"),
                                                  verify=verify)
                    except HTTPError as e:
                        errors.add(
                            url, dist,
                            f"Could not access Packages file at {e.response.url}: {e}"
                        )
                        continue

                    _check_apt_packages(url, dist, comp, arch, packages, errors, verify=verify)

        except HTTPError as e:
            errors.add(
                url, dist,
                f"Error when attempting to access {e.response.url}: {e}"
            )
