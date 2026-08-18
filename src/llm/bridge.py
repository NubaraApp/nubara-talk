"""Puente al LLM por CLI.

Se invoca por `subprocess`, no por API HTTP: así no consume créditos y el repo no maneja
ninguna clave. Ver ADR-008.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from src.llm.prompts import CliSpec

log = logging.getLogger(__name__)

# Las CLI interactivas escupen spinners y control de cursor aunque stdout no sea un TTY.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

# Señales de que la CLI está instalada pero no autenticada. No es un fallo de red ni un
# bug nuestro: el usuario tiene que ir a loguearse.
_AUTH_HINTS = ("not logged in", "please run /login", "unauthorized", "not authenticated")


class LlmError(RuntimeError):
    pass


class NotAuthenticated(LlmError):
    pass


@dataclass(frozen=True, slots=True)
class LlmResponse:
    cli_id: str
    text: str
    elapsed: float


def _clean(raw: str) -> str:
    text = _ANSI.sub("", raw)
    # El spinner reescribe la misma línea con \r: queda la última versión de cada una.
    text = "\n".join(line.split("\r")[-1] for line in text.splitlines())
    return text.strip()


def available(spec: CliSpec) -> bool:
    return shutil.which(spec.command[0]) is not None


class LlmCli:
    def __init__(self, spec: CliSpec, timeout_s: float = 120.0) -> None:
        self.spec = spec
        self.timeout_s = timeout_s

    def run(self, prompt: str, on_chunk: Callable[[str], None] | None = None) -> LlmResponse:
        """Manda el prompt por stdin y devuelve la respuesta limpia.

        Por stdin y no por argv: un fragmento de reunión largo excede el límite de tamaño
        de los argumentos, y pasarlo como argumento obliga a preocuparse por el quoting.
        Nunca con `shell=True`.

        Si se pasa `on_chunk`, se llama con cada trozo nuevo de texto a medida que sale.
        La respuesta completa tarda lo mismo, pero se puede empezar a leer antes.
        """
        if not available(self.spec):
            raise LlmError(
                f"{self.spec.command[0]!r} no está instalado o no está en el PATH.\n"
                f"  Configurá otra CLI en config/prompts.yaml, en cli.candidatos."
            )

        t0 = time.monotonic()
        proc = subprocess.Popen(
            list(self.spec.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # stderr se drena en un hilo: si el proceso lo llena y nadie lee, se traba.
        errors: list[str] = []
        drain = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
        drain.start()

        raw, shown = "", ""
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()

            deadline = t0 + self.timeout_s
            while True:
                piece = proc.stdout.read(64)
                if not piece:
                    break
                raw += piece
                if on_chunk is not None:
                    # Limpiar sobre el acumulado y mandar sólo lo nuevo: una secuencia
                    # ANSI puede quedar partida entre dos lecturas, y limpiar cada trozo
                    # por separado dejaría la mitad de la secuencia en el texto.
                    clean = _clean(raw)
                    if len(clean) > len(shown):
                        on_chunk(clean[len(shown):])
                        shown = clean
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(self.spec.command[0], self.timeout_s)

            proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait(timeout=5)
            raise LlmError(
                f"{self.spec.id}: no respondió en {self.timeout_s:.0f} s. "
                f"Subí cli.timeout_s en config/prompts.yaml, o usá un modelo más chico."
            ) from exc
        finally:
            drain.join(timeout=2)

        elapsed = time.monotonic() - t0
        out = _clean(raw)
        err = _clean("".join(x for x in errors if x))
        blob = f"{out}\n{err}".lower()

        if any(hint in blob for hint in _AUTH_HINTS):
            raise NotAuthenticated(
                f"{self.spec.id}: la CLI está instalada pero no autenticada.\n"
                f"  Logueate en una terminal interactiva y volvé a intentar.\n"
                f"  Dijo: {(err or out).splitlines()[0] if (err or out) else '—'}"
            )

        if proc.returncode != 0:
            raise LlmError(
                f"{self.spec.id}: salió con código {proc.returncode}.\n  {err or out or '—'}"
            )

        if not out:
            raise LlmError(f"{self.spec.id}: respondió vacío. stderr: {err or '—'}")

        return LlmResponse(cli_id=self.spec.id, text=out, elapsed=elapsed)


def pick(candidates: tuple[CliSpec, ...], prefer: str | None = None) -> CliSpec:
    """Elige la primera CLI instalada, o la pedida por id."""
    if prefer is not None:
        for spec in candidates:
            if spec.id == prefer:
                return spec
        known = ", ".join(c.id for c in candidates)
        raise LlmError(f"No hay una CLI con id {prefer!r}. Conocidas: {known}")

    for spec in candidates:
        if available(spec):
            return spec

    known = ", ".join(f"{c.id} ({c.command[0]})" for c in candidates)
    raise LlmError(f"Ninguna CLI de cli.candidatos está instalada. Probé: {known}")
