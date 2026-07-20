# --------------------------- in1Utils/cfgUtils.py --------------------------- #
#
# Purpose :
#   Unified configuration and logging utilities for the Avalanche Scenario Mapper.
#
#   Provides configuration loading with local overrides, unified logging setup,
#   relative path formatting for compact log output, and a timing decorator for
#   performance diagnostics.
#
# Consistent with Avalanche Scenario Model Chain style :
#   - Logging format and levels
#   - local_<config>.ini override behavior
#   - relPath() for short log references
#
# Used by :
#   - runAvaScenMapper.py
#   - in1Utils/mapperUtils.py
#
# Author :
#   Christoph Hesselbach
#
# Institution :
#   Austrian Research Centre for Forests (BFW)
#   Department of Natural Hazards | Snow and Avalanche Unit
#
# Date & Version :
#   2026-03 - 1.1
#
# ------------------------------------------------------------------------------ #

import os
import sys
import time
import json
import logging
import configparser
from pathlib import Path


# ------------------ Small config helpers ------------------ #

def getLogLevel(cfg: configparser.ConfigParser) -> int:
    """
    Resolve log level from configuration.

    Reads:
        [WORKFLOW] logLevel = DEBUG | INFO | WARNING | ERROR
    """
    levelName = cfg.get("WORKFLOW", "logLevel", fallback="INFO").upper().strip()
    return getattr(logging, levelName, logging.INFO)


def getConfiguredPath(cfg: configparser.ConfigParser, section: str, key: str) -> Path | None:
    """
    Read a configured path from the INI and expand env vars + user home.
    Returns None for missing or empty values.
    """
    if not cfg.has_option(section, key):
        return None

    raw = cfg.get(section, key, fallback="").strip()
    if not raw:
        return None

    return Path(os.path.expandvars(raw)).expanduser()


def getDefaultMapperLogDir(cfg: configparser.ConfigParser) -> Path:
    """
    Determine a sensible default log directory without depending on mapperUtils.

    Use [PATHS] avaScenMapsDir, falling back to the current directory.
    """
    avaScenMapsDir = getConfiguredPath(cfg, "PATHS", "avaScenMapsDir")
    if avaScenMapsDir is not None:
        return avaScenMapsDir

    return Path.cwd()


# ------------------ Logging setup ------------------ #

def setupMapperLogging(
    cfg: configparser.ConfigParser,
    logSubdir: str | None = None,
    logFilePrefix: str = "runAvaScenMapper",
) -> Path:
    """
    Configure logging for mapper-style standalone modules.

    - Console: no timestamps, compact format
    - File: timestamps, detailed logs
    - Output: log file stored in avaScenMapsDir

    Parameters
    ----------
    cfg : ConfigParser
        Loaded mapper configuration.
    logSubdir : str, optional
        Optional subfolder for log placement.
    logFilePrefix : str, optional
        Prefix of the created log filename.

    Returns
    -------
    Path
        Path to the created log file.
    """
    level = getLogLevel(cfg)
    log = logging.getLogger(__name__)

    logDir = getDefaultMapperLogDir(cfg)
    if logSubdir:
        logDir = logDir / logSubdir
    logDir.mkdir(parents=True, exist_ok=True)

    logPath = logDir / f"{logFilePrefix}_{time.strftime('%Y%m%d_%H%M%S')}.log"

    rootLogger = logging.getLogger()
    for handler in list(rootLogger.handlers):
        rootLogger.removeHandler(handler)

    consoleFmt = logging.Formatter("[%(levelname)s] %(name)s - %(message)s")
    consoleHandler = logging.StreamHandler(sys.stdout)
    consoleHandler.setFormatter(consoleFmt)
    consoleHandler.setLevel(level)

    fileFmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fileHandler = logging.FileHandler(logPath, mode="w", encoding="utf-8")
    fileHandler.setFormatter(fileFmt)
    fileHandler.setLevel(level)

    rootLogger.setLevel(level)
    rootLogger.addHandler(consoleHandler)
    rootLogger.addHandler(fileHandler)

    log.info("Log file created at: %s", logPath)
    return logPath


# ------------------ INI reading ------------------ #

def readCfg(cfgPath: Path) -> configparser.ConfigParser:
    """
    Read main INI configuration file and optional local override.

    Expected structure:
        avaScenMapperCfg.ini
        local_avaScenMapperCfg.ini  (optional)

    The local file, if present, overrides parameters from the main file.
    """
    cfg = configparser.ConfigParser()
    log = logging.getLogger(__name__)

    if not cfgPath.exists():
        raise FileNotFoundError(f"Missing configuration file: {cfgPath}")

    with cfgPath.open("r", encoding="utf-8") as f:
        cfg.read_file(f)
    log.info("Loaded main configuration: %s", cfgPath.name)

    localPath = cfgPath.parent / f"local_{cfgPath.name}"
    if localPath.exists():
        cfg.read(localPath, encoding="utf-8")
        log.info("Loaded local override: %s", localPath.name)
    else:
        log.info("No local override found (%s)", localPath.name)

    return cfg


def writeConfigSnapshot(
    cfg: configparser.ConfigParser,
    outputPath: Path,
    scenarioName: str,
    filterSection: str | None = None,
) -> Path:
    """Write the effective INI configuration used for one scenario as JSON."""
    outputPath = Path(outputPath)
    outputPath.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scenarioName": scenarioName,
        "filterSection": filterSection,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "config": {
            section: dict(cfg[section])
            for section in cfg.sections()
        },
    }
    with outputPath.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    logging.getLogger(__name__).info("Wrote scenario config snapshot: %s", outputPath)
    return outputPath


# ------------------ Path helper ------------------ #

def relPath(path: Path, baseDir: Path) -> str:
    """
    Return a relative path string for concise log messages.
    Falls back to absolute path if relative conversion fails.
    """
    try:
        return os.path.relpath(path, baseDir)
    except Exception:
        return str(path)
