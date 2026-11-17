"""Amazon Payments vs Settlement reconciliation pipeline."""
import os as _os


def _expose_libpq():
    """psycopg needs libpq. If psycopg[binary] cannot be loaded (e.g. blocked by
    Windows Application Control) fall back to the libpq shipped with pgserver."""
    try:
        import importlib.util

        spec = importlib.util.find_spec("pgserver")
        if not spec or not spec.origin:
            return
        bindir = _os.path.join(_os.path.dirname(spec.origin), "pginstall", "bin")
        if _os.path.isdir(bindir):
            _os.environ["PATH"] = bindir + _os.pathsep + _os.environ.get("PATH", "")
            if hasattr(_os, "add_dll_directory"):
                _os.add_dll_directory(bindir)
    except Exception:  # pragma: no cover
        pass


_expose_libpq()
