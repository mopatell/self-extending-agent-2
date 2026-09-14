"""What agent-written code may import. Mirrors the packages baked into the sandbox image."""

# pip name -> import names it provides
ALLOWED_PACKAGES = {
    "requests": {"requests"},
    "beautifulsoup4": {"bs4"},
    "pandas": {"pandas", "numpy"},
    "pypdf": {"pypdf"},
    "pyyaml": {"yaml"},
    "python-dateutil": {"dateutil"},
}

# Modules that would let a tool escape the approval gate (run commands, open sockets, ...).
FORBIDDEN_MODULES = {"subprocess", "socket", "ctypes", "multiprocessing", "pty", "signal"}

# Calling these bypasses static checks entirely.
FORBIDDEN_CALLS = {"eval", "exec", "__import__", "compile"}
FORBIDDEN_ATTRS = {("os", "system"), ("os", "popen"), ("os", "execv"), ("os", "execvp"), ("os", "spawn")}

# Importing these means the tool talks to the network, so it must declare network=True.
NETWORK_MODULES = {"requests", "urllib", "http", "socket", "ftplib", "smtplib"}
