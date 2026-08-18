"""Traducción local con NLLB-200 distilled 600M sobre CTranslate2.

Es el traductor del subtitulado en vivo: corre en la máquina, sin red, y tiene que
caber en el presupuesto de latencia de un turno. La CLI del LLM (F2) queda para
"traducí esto bien" bajo demanda. Ver ADR-014.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MODEL_DIR = Path("models/nllb-200-distilled-600M-ct2")

# NLLB habla FLORES-200, Whisper devuelve ISO 639-1. Sin traducir entre los dos, el
# modelo recibe un código que no conoce y genera basura en vez de fallar.
FLORES = {
    "en": "eng_Latn",
    "es": "spa_Latn",
    "pt": "por_Latn",
    "fr": "fra_Latn",
    "de": "deu_Latn",
    "it": "ita_Latn",
    "ca": "cat_Latn",
    "nl": "nld_Latn",
    "ja": "jpn_Jpan",
    "zh": "zho_Hans",
}


def flores(code: str) -> str:
    """Traduce un código de Whisper a uno de FLORES-200, o lo deja pasar si ya lo es."""
    if "_" in code:
        return code
    try:
        return FLORES[code.lower()]
    except KeyError:
        raise ValueError(
            f"Idioma '{code}' sin mapeo a FLORES-200. Agregalo a FLORES en "
            f"src/translate/nllb.py; la lista completa está en el README de NLLB-200."
        ) from None


class Translator:
    """Envoltorio del modelo. Una instancia por proceso: el modelo pesa y es reentrante.

    `ctranslate2.Translator` sí es seguro entre hilos, a diferencia del VAD (ADR-010),
    pero igual se comparte una sola instancia para no duplicar 600 MB en memoria.
    """

    def __init__(
        self,
        model_dir: Path | str = DEFAULT_MODEL_DIR,
        device: str = "cpu",
        compute_type: str = "int8",
        intra_threads: int = 4,
    ) -> None:
        import ctranslate2
        from transformers import AutoTokenizer

        self.model_dir = Path(model_dir)
        if not self.model_dir.exists():
            raise FileNotFoundError(
                f"No está el modelo en {self.model_dir}. Convertilo con:\n"
                f"  .venv/bin/ct2-transformers-converter \\\n"
                f"    --model facebook/nllb-200-distilled-600M \\\n"
                f"    --output_dir {self.model_dir} --quantization int8 \\\n"
                f"    --copy_files tokenizer.json tokenizer_config.json special_tokens_map.json "
                f"sentencepiece.bpe.model"
            )

        t0 = time.monotonic()
        self._engine = ctranslate2.Translator(
            str(self.model_dir),
            device=device,
            compute_type=compute_type,
            intra_threads=intra_threads,
        )
        self._tok = AutoTokenizer.from_pretrained(str(self.model_dir))
        self.load_time = time.monotonic() - t0
        self._src_lang: str | None = None
        self._lock = threading.Lock()
        log.info("NLLB cargado desde %s en %.1fs", self.model_dir, self.load_time)

    def _encode(self, text: str, src: str) -> list[str]:
        # El tokenizer inyecta el token de idioma de origen según `src_lang`, así que hay
        # que fijarlo antes de encodear y no pasarlo como argumento suelto.
        if self._src_lang != src:
            self._tok.src_lang = src
            self._src_lang = src
        return self._tok.convert_ids_to_tokens(self._tok.encode(text))

    def translate(self, text: str, src: str, tgt: str, beam_size: int = 1) -> str:
        """Traduce un fragmento. Devuelve cadena vacía si entra cadena vacía.

        `beam_size=1` (greedy) a propósito: para subtitular en vivo, un beam de 4 cuesta
        el triple de tiempo y cambia poco el resultado en frases cortas de reunión.
        """
        out = self.translate_many([text], src, tgt, beam_size=beam_size)
        return out[0]

    def translate_many(
        self, texts: list[str], src: str, tgt: str, beam_size: int = 1
    ) -> list[str]:
        """Traduce un lote. Un lote de N cuesta bastante menos que N llamadas sueltas."""
        src, tgt = flores(src), flores(tgt)
        keep = [i for i, t in enumerate(texts) if t.strip()]
        if not keep:
            return ["" for _ in texts]

        with self._lock:
            batch = [self._encode(texts[i], src) for i in keep]
            results = self._engine.translate_batch(
                batch,
                target_prefix=[[tgt]] * len(batch),
                beam_size=beam_size,
                max_batch_size=8,
            )

        out = ["" for _ in texts]
        for i, res in zip(keep, results):
            # La primera pieza de la hipótesis es el token de idioma que le dimos de
            # prefijo; si no se descarta, aparece "spa_Latn" al principio del texto.
            tokens = res.hypotheses[0][1:]
            out[i] = self._tok.decode(self._tok.convert_tokens_to_ids(tokens))
        return out

    def warmup(self, src: str = "en", tgt: str = "es") -> float:
        """Fuerza la primera inferencia, que siempre es la más lenta."""
        t0 = time.monotonic()
        self.translate("Hello, can you hear me?", src, tgt)
        return time.monotonic() - t0
