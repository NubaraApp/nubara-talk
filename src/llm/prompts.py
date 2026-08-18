"""Carga de las plantillas de prompts desde config/prompts.yaml."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("config/prompts.yaml")

# Lo único que se le pasa a una plantilla. Cualquier otro marcador es un typo, y es mejor
# que falle al cargar el archivo que a mitad de una reunión.
# `respuesta` es la respuesta corta que ya se dio, para que una plantilla pueda
# ampliarla en vez de empezar de cero.
PLACEHOLDERS = frozenset({"fragmento", "contexto", "idioma", "respuesta"})


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    id: str
    nombre: str
    descripcion: str
    plantilla: str
    # Etiqueta corta para los botones que aparecen encima de cada frase. Si no está,
    # se usa `nombre`, que suele ser demasiado largo para un botón chico.
    corto: str = ""
    # "fragmento" (contestar en el idioma de lo que se dijo) o "propio" (en el idioma
    # en el que leo). Traducir al mismo idioma del original no traduce nada.
    idioma: str = "propio"
    # CLI preferida para esta plantilla. Si no está instalada, se usa la de siempre.
    cli: str = ""
    # Id de la plantilla que amplía a esta. La ventana la dispara sola apenas
    # termina la respuesta corta, para que la larga se vaya escribiendo mientras
    # se lee la corta.
    ampliar_con: str = ""

    @property
    def usa_respuesta(self) -> bool:
        """Si la plantilla necesita una respuesta previa para tener sentido.

        Las que amplían algo ya contestado no se pueden ofrecer sueltas: sin
        `{respuesta}` el prompt queda con un hueco vacío.
        """
        return "{respuesta}" in self.plantilla

    def render(
        self,
        fragmento: str,
        contexto: str = "",
        idioma: str = "español",
        respuesta: str = "",
    ) -> str:
        return self.plantilla.format(
            fragmento=fragmento, contexto=contexto, idioma=idioma, respuesta=respuesta
        )


@dataclass(frozen=True, slots=True)
class CliSpec:
    id: str
    command: tuple[str, ...]
    local: bool


@dataclass(frozen=True, slots=True)
class Config:
    prompts: dict[str, PromptTemplate]
    candidates: tuple[CliSpec, ...]
    timeout_s: float


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


def load(path: Path | str = DEFAULT_CONFIG) -> Config:
    path = Path(path)
    if not path.exists():
        raise RuntimeError(f"No existe {path}. Es donde viven las plantillas de prompts.")

    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}

    prompts: dict[str, PromptTemplate] = {}
    for entry in raw.get("prompts") or []:
        missing = {"id", "nombre", "plantilla"} - set(entry)
        if missing:
            raise RuntimeError(f"{path}: a un prompt le faltan campos: {sorted(missing)}")

        unknown = _placeholders(entry["plantilla"]) - PLACEHOLDERS
        if unknown:
            raise RuntimeError(
                f"{path}: el prompt {entry['id']!r} usa marcadores que no existen: "
                f"{sorted(unknown)}. Disponibles: {sorted(PLACEHOLDERS)}"
            )

        prompts[entry["id"]] = PromptTemplate(
            id=entry["id"],
            nombre=entry["nombre"],
            descripcion=entry.get("descripcion", ""),
            plantilla=entry["plantilla"],
            corto=entry.get("corto") or entry["nombre"],
            idioma=entry.get("idioma", "propio"),
            cli=entry.get("cli", ""),
            ampliar_con=entry.get("ampliar_con", ""),
        )

    for template in prompts.values():
        if template.ampliar_con and template.ampliar_con not in prompts:
            raise RuntimeError(
                f"{path}: el prompt {template.id!r} amplía con "
                f"{template.ampliar_con!r}, que no existe."
            )
        if template.idioma not in ("fragmento", "propio"):
            raise RuntimeError(
                f"{path}: el prompt {template.id!r} tiene idioma={template.idioma!r}. "
                f"Sólo valen 'fragmento' y 'propio'."
            )

    if not prompts:
        raise RuntimeError(f"{path}: no hay ningún prompt definido.")

    cli = raw.get("cli") or {}
    candidates = tuple(
        CliSpec(
            id=c["id"],
            command=tuple(c["comando"]),
            local=bool(c.get("local", False)),
        )
        for c in cli.get("candidatos") or []
    )
    if not candidates:
        raise RuntimeError(f"{path}: no hay ninguna CLI en cli.candidatos.")

    return Config(
        prompts=prompts,
        candidates=candidates,
        timeout_s=float(cli.get("timeout_s", 120)),
    )
