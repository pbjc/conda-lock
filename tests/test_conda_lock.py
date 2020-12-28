import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys

from typing import Any, MutableSequence

import pytest

from conda_lock.conda_lock import (
    PathLike,
    _ensureconda,
    aggregate_lock_specs,
    conda_env_override,
    create_lockfile_from_spec,
    determine_conda_executable,
    main,
    parse_meta_yaml_file,
    run_lock,
)
from conda_lock.src_parser import LockSpecification
from conda_lock.src_parser.environment_yaml import parse_environment_file
from conda_lock.src_parser.pyproject_toml import (
    parse_flit_pyproject_toml,
    parse_poetry_pyproject_toml,
    poetry_version_to_conda_version,
    to_match_spec,
)


@pytest.fixture(autouse=True)
def logging_setup(caplog):
    caplog.set_level(logging.DEBUG)


@pytest.fixture
def gdal_environment():
    return pathlib.Path(__file__).parent.joinpath("gdal").joinpath("environment.yml")


@pytest.fixture
def zlib_environment():
    return pathlib.Path(__file__).parent.joinpath("zlib").joinpath("environment.yml")


@pytest.fixture
def meta_yaml_environment():
    return pathlib.Path(__file__).parent.joinpath("test-recipe").joinpath("meta.yaml")


@pytest.fixture
def poetry_pyproject_toml():
    return (
        pathlib.Path(__file__).parent.joinpath("test-poetry").joinpath("pyproject.toml")
    )


@pytest.fixture
def flit_pyproject_toml():
    return (
        pathlib.Path(__file__).parent.joinpath("test-flit").joinpath("pyproject.toml")
    )


@pytest.fixture(
    scope="function",
    params=[
        pytest.param(True, id="--dev-dependencies"),
        pytest.param(False, id="--no-dev-dependencies"),
    ],
)
def include_dev_dependencies(request: Any) -> bool:
    return request.param


def test_parse_environment_file(gdal_environment):
    res = parse_environment_file(gdal_environment, "linux-64")
    assert all(x in res.specs for x in ["python >=3.7,<3.8", "gdal"])
    assert all(x in res.channels for x in ["conda-forge", "defaults"])


def test_parse_meta_yaml_file(meta_yaml_environment, include_dev_dependencies):
    res = parse_meta_yaml_file(
        meta_yaml_environment,
        platform="linux-64",
        include_dev_dependencies=include_dev_dependencies,
    )
    assert all(x in res.specs for x in ["python", "numpy"])
    # Ensure that this dep specified by a python selector is ignored
    assert "enum34" not in res.specs
    # Ensure that this platform specific dep is included
    assert "zlib" in res.specs
    assert ("pytest" in res.specs) == include_dev_dependencies


def test_parse_poetry(poetry_pyproject_toml, include_dev_dependencies):
    res = parse_poetry_pyproject_toml(
        poetry_pyproject_toml,
        platform="linux-64",
        include_dev_dependencies=include_dev_dependencies,
    )

    assert "requests[version='>=2.13.0,<3.0.0']" in res.specs
    assert "toml[version='>=0.10']" in res.specs
    assert ("pytest[version='>=5.1.0,<5.2.0']" in res.specs) == include_dev_dependencies
    assert res.channels == ["defaults"]


def test_parse_flit(flit_pyproject_toml, include_dev_dependencies):
    res = parse_flit_pyproject_toml(
        flit_pyproject_toml,
        platform="linux-64",
        include_dev_dependencies=include_dev_dependencies,
    )

    assert "requests[version='>=2.13.0']" in res.specs
    assert "toml[version='>=0.10']" in res.specs
    # test deps
    assert ("pytest[version='>=5.1.0']" in res.specs) == include_dev_dependencies
    assert res.channels == ["defaults"]


def test_run_lock(monkeypatch, zlib_environment, conda_exe):
    monkeypatch.chdir(zlib_environment.parent)
    run_lock([zlib_environment], conda_exe=conda_exe)


@pytest.mark.parametrize(
    "package,version,url_pattern",
    [
        ("python", ">=3.6,<3.7", "/python-3.6"),
        ("python", "~3.6", "/python-3.6"),
        ("python", "^2.7", "/python-2.7"),
    ],
)
def test_poetry_version_parsing_constraints(package, version, url_pattern):
    _conda_exe = determine_conda_executable("conda", mamba=False, micromamba=False)
    spec = LockSpecification(
        specs=[to_match_spec(package, poetry_version_to_conda_version(version))],
        channels=["conda-forge"],
        platform="linux-64",
    )
    lockfile_contents = create_lockfile_from_spec(
        conda=_conda_exe, channels=spec.channels, spec=spec
    )

    for line in lockfile_contents:
        if url_pattern in line:
            break
    else:
        raise ValueError(f"could not find {package} {version}")


def test_aggregate_lock_specs():
    gpu_spec = LockSpecification(
        specs=["pytorch"],
        channels=["pytorch", "conda-forge"],
        platform="linux-64",
    )

    base_spec = LockSpecification(
        specs=["python =3.7"],
        channels=["conda-forge"],
        platform="linux-64",
    )

    assert (
        aggregate_lock_specs([gpu_spec, base_spec]).env_hash()
        == LockSpecification(
            specs=["pytorch", "python =3.7"],
            channels=["pytorch", "conda-forge"],
            platform="linux-64",
        ).env_hash()
    )

    assert (
        aggregate_lock_specs([base_spec, gpu_spec]).env_hash()
        == LockSpecification(
            specs=["pytorch", "python =3.7"],
            channels=["conda-forge"],
            platform="linux-64",
        ).env_hash()
    )


@pytest.fixture(
    scope="session",
    params=[
        pytest.param("conda"),
        pytest.param("mamba"),
        pytest.param("micromamba"),
        pytest.param("conda_exe"),
    ],
)
def conda_exe(request):
    kwargs = dict(
        mamba=False,
        micromamba=False,
        conda=False,
        conda_exe=False,
    )
    kwargs[request.param] = True
    _conda_exe = _ensureconda(**kwargs)

    if _conda_exe is not None:
        return _conda_exe
    raise pytest.skip(f"{request.param} is not installed")


def _check_package_installed(package: str, prefix: str):
    import glob

    files = list(glob.glob(f"{prefix}/conda-meta/{package}-*.json"))
    assert len(files) >= 1
    # TODO: validate that all the files are in there
    for fn in files:
        data = json.load(open(fn))
        for expected_file in data["files"]:
            assert (pathlib.Path(prefix) / pathlib.Path(expected_file)).exists()
    return True


def test_install(tmp_path, conda_exe, zlib_environment):
    package = "zlib"
    platform = "linux-64"

    lock_filename_format = "conda-{platform}-{dev-dependencies}.lock"
    lock_filename = "conda-linux-64-True.lock"
    try:
        os.remove(lock_filename)
    except OSError:
        pass

    from click.testing import CliRunner

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(
        main,
        [
            "lock",
            "--conda",
            conda_exe,
            "-p",
            platform,
            "-f",
            zlib_environment,
            "--filename-format",
            lock_filename_format,
        ],
    )
    assert result.exit_code == 0

    env_name = "test_env"
    result = runner.invoke(
        main,
        [
            "install",
            "--conda",
            conda_exe,
            "--prefix",
            tmp_path / env_name,
            lock_filename,
        ],
    )
    print(result.stdout, file=sys.stdout)
    print(result.stderr, file=sys.stderr)
    logging.debug(
        "lockfile contents: \n\n=======\n%s\n\n==========",
        pathlib.Path(lock_filename).read_text(),
    )
    assert result.exit_code == 0
    assert _check_package_installed(
        package=package,
        prefix=str(tmp_path / env_name),
    ), f"Package {package} does not exist in {tmp_path} environment"
