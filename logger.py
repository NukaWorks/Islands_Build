"""
Logging helpers: coloured, timestamped output with rich-style sections.
Falls back to plain text if 'rich' is not installed.
"""
import sys
import time
from datetime import datetime

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    _HAS_RICH = True
    _console     = Console()
    _console_err = Console(stderr=True)
except ImportError:
    _HAS_RICH = False
    _console = None
    _console_err = None

# ANSI fallback colours
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_BLUE   = "\033[94m"
_DIM    = "\033[2m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def section(title: str) -> None:
    if _HAS_RICH:
        _console.rule(f"[bold cyan]{title}[/bold cyan]")
    else:
        line = "─" * 60
        print(f"\n{_CYAN}{line}\n  {title}\n{line}{_RESET}\n")


def info(msg: str) -> None:
    if _HAS_RICH:
        _console.print(f"[dim]{_ts()}[/dim]  [blue]ℹ[/blue]  {msg}")
    else:
        print(f"{_DIM}{_ts()}{_RESET}  {_BLUE}ℹ{_RESET}  {msg}")


def success(msg: str) -> None:
    if _HAS_RICH:
        _console.print(f"[dim]{_ts()}[/dim]  [bold green]✔[/bold green]  {msg}")
    else:
        print(f"{_DIM}{_ts()}{_RESET}  {_GREEN}{_BOLD}✔{_RESET}  {msg}")


def warn(msg: str) -> None:
    if _HAS_RICH:
        _console.print(f"[dim]{_ts()}[/dim]  [bold yellow]⚠[/bold yellow]  {msg}")
    else:
        print(f"{_DIM}{_ts()}{_RESET}  {_YELLOW}{_BOLD}⚠{_RESET}  {msg}")


def error(msg: str) -> None:
    if _HAS_RICH:
        _console_err.print(f"[dim]{_ts()}[/dim]  [bold red]✖[/bold red]  {msg}")
    else:
        print(f"{_DIM}{_ts()}{_RESET}  {_RED}{_BOLD}✖{_RESET}  {msg}", file=sys.stderr)


def step(index: int, total: int, msg: str) -> None:
    label = f"[{index}/{total}]"
    if _HAS_RICH:
        _console.print(f"[dim]{_ts()}[/dim]  [bold magenta]{label}[/bold magenta]  {msg}")
    else:
        print(f"{_DIM}{_ts()}{_RESET}  {_BOLD}{label}{_RESET}  {msg}")


def banner(title: str, subtitle: str = "") -> None:
    if _HAS_RICH:
        text = Text(title, style="bold cyan")
        if subtitle:
            text.append(f"\n{subtitle}", style="dim")
        _console.print(Panel(text, border_style="cyan"))
    else:
        print(f"\n{_CYAN}{'═' * 60}")
        print(f"  {_BOLD}{title}{_RESET}{_CYAN}")
        if subtitle:
            print(f"  {_DIM}{subtitle}{_RESET}{_CYAN}")
        print(f"{'═' * 60}{_RESET}\n")


def duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s"

