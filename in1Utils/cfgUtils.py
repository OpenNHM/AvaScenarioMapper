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
#   2025-11 - 1.0
#
# ------------------------------------------------------------------------------ #


import os
import time
import logging
import configparser
from pathlib import Path
from functools import wraps


# ------------------ Logging setup ------------------ #

def setupLogging(cfg: configparser.ConfigParser) -> None:
    """
    Initialize unified Avalanche Scenario Mapper logging based on configuration settings.

    Reads the log level from:
        [WORKFLOW] logLevel = INFO | DEBUG | WARNING | ERROR
    and applies a consistent format across all modules.
    """
    levelName = cfg.get("WORKFLOW", "logLevel", fallback="INFO").upper()
    level = getattr(logging, levelName, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log = logging.getLogger(__name__)
    log.info("Logging initialized at level: %s", levelName)


# ------------------ Unified Mapper Logging ------------------ #

def setupMapperLogging(cfg, log_subdir: str = None, base_dir_key: str = "avaScenMapsDir") -> Path:
    """
    Configure logging for mapper-style standalone modules.
    - Console: no timestamps, clean format
    - File: full timestamps, detailed logs
    - Output: log file stored in the 13_avaScenMaps directory (by default)

    Parameters
    ----------
    cfg : ConfigParser
        Loaded mapper configuration.
    log_subdir : str, optional
        Optional subfolder under base_dir_key for log placement.
    base_dir_key : str, optional
        Key from resolvePaths() used as base (default: avaScenMapsDir).

    Returns
    -------
    Path
        Path to the created log file.
    """
    import sys
    from in1Utils import mapperUtils  # local import to avoid circular load
    log = logging.getLogger(__name__)

    # --- Determine base directory ---
    try:
        paths = mapperUtils.resolvePaths(cfg)
        log_dir = Path(paths.get(base_dir_key, Path.cwd()))
    except Exception:
        log.warning("Could not resolve mapper paths; using current working directory.")
        log_dir = Path.cwd()

    if log_subdir:
        log_dir = log_dir / log_subdir
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / f"runAvaScenMapper_{time.strftime('%Y%m%d_%H%M%S')}.log"

    # --- Clear existing handlers ---
    root_logger = logging.getLogger()
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    # --- Console handler (no timestamps) ---
    console_fmt = logging.Formatter("[%(levelname)s] %(name)s - %(message)s")
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_fmt)
    console_handler.setLevel(logging.INFO)

    # --- File handler (with timestamps) ---
    file_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(file_fmt)
    file_handler.setLevel(logging.INFO)

    # --- Apply handlers ---
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    log.info("Log file created at: %s", log_path)
    return log_path


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

    # Load main configuration
    with cfgPath.open("r", encoding="utf-8") as f:
        cfg.read_file(f)
    log.info("Loaded main configuration: %s", cfgPath.name)

    # Load optional local override
    localPath = cfgPath.parent / f"local_{cfgPath.name}"
    if localPath.exists():
        cfg.read(localPath, encoding="utf-8")
        log.info("Loaded local override: %s", localPath.name)
    else:
        log.info("No local override found (%s)", localPath.name)

    return cfg


# ------------------ Path helper ------------------ #

def relPath(path: Path, baseDir: Path) -> str:
    """
    Return a relative path string (for concise log messages).

    Parameters
    ----------
    path : Path
        Full file or directory path.
    baseDir : Path
        Base directory to which the relative path is computed.

    Returns
    -------
    str
        Path relative to baseDir if possible, otherwise absolute.
    """
    try:
        return os.path.relpath(path, baseDir)
    except Exception:
        return str(path)


# ------------------ Timing helper ------------------ #

def timeIt(func):
    """
    Decorator for timing function execution with INFO-level logging.

    Example
    -------
    @timeIt
    def runStep(...):
        ...
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        log = logging.getLogger(func.__module__)
        t0 = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            dt = time.perf_counter() - t0
            log.info("%s finished in %.2fs", func.__name__, dt)
    return wrapper
