"""Unabhängige Zeitstempel nach RFC 3161.

An den Zeitstempeldienst wird ausschließlich eine SHA-256-Prüfsumme geschickt –
der Inhalt der Recherche verlässt den Server nicht. Der Dienst signiert
"diese Prüfsumme existierte zum Zeitpunkt X". Die Antwort (.tsr) lässt sich
später von jedem mit `openssl ts -verify` prüfen.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from asn1crypto import algos, core, tsp


class TimestampError(Exception):
    pass


@dataclass
class Timestamp:
    url: str
    gen_time: datetime
    token: bytes  # vollständige TimeStampResp (DER), als .tsr speicherbar


def build_request(digest: bytes) -> tuple[bytes, int]:
    if len(digest) != 32:
        raise ValueError("SHA-256-Digest erwartet")
    nonce = secrets.randbits(63)
    req = tsp.TimeStampReq({
        "version": "v1",
        "message_imprint": tsp.MessageImprint({
            "hash_algorithm": algos.DigestAlgorithm({"algorithm": "sha256", "parameters": core.Null()}),
            "hashed_message": digest,
        }),
        "nonce": nonce,
        "cert_req": True,
    })
    return req.dump(), nonce


def parse_response(data: bytes, digest: bytes, nonce: int | None) -> datetime:
    """Prüft Status, Prüfsumme und Nonce der Antwort und liefert den Zeitpunkt (UTC)."""
    try:
        resp = tsp.TimeStampResp.load(data)
        status = resp["status"]["status"].native
    except Exception as e:
        raise TimestampError(f"Antwort ist kein gültiger RFC-3161-Zeitstempel: {e}") from e
    if status not in ("granted", "granted_with_mods"):
        text = resp["status"]["status_string"].native
        raise TimestampError(f"Zeitstempel abgelehnt: {status} {text or ''}".strip())
    try:
        tst_info = resp["time_stamp_token"]["content"]["encap_content_info"]["content"].parsed
        imprint = tst_info["message_imprint"]["hashed_message"].native
        gen_time = tst_info["gen_time"].native
        resp_nonce = tst_info["nonce"].native
    except Exception as e:
        raise TimestampError(f"Zeitstempel unlesbar: {e}") from e
    if imprint != digest:
        raise TimestampError("Zeitstempel bezieht sich auf eine andere Prüfsumme")
    if nonce is not None and resp_nonce is not None and resp_nonce != nonce:
        raise TimestampError("Zeitstempel-Antwort passt nicht zur Anfrage (Nonce)")
    if gen_time.tzinfo is None:
        gen_time = gen_time.replace(tzinfo=timezone.utc)
    return gen_time.astimezone(timezone.utc)


def stamp(digest: bytes, urls: list[str], timeout: float = 30) -> Timestamp:
    """Fragt die Dienste der Reihe nach an, bis einer einen Zeitstempel liefert."""
    errors = []
    for url in urls:
        req, nonce = build_request(digest)
        try:
            r = httpx.post(url, content=req, timeout=timeout,
                           headers={"Content-Type": "application/timestamp-query"})
            r.raise_for_status()
            gen_time = parse_response(r.content, digest, nonce)
            return Timestamp(url=url, gen_time=gen_time, token=r.content)
        except (httpx.HTTPError, TimestampError) as e:
            errors.append(f"{url}: {e}")
    raise TimestampError("; ".join(errors) or "Kein Zeitstempeldienst konfiguriert")
