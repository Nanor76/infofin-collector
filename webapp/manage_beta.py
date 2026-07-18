from __future__ import annotations

import argparse
import getpass
import json
import sys

from webapp.beta_access import hash_password, parse_beta_users


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Génère le JSON secret des comptes de la bêta InfoFin."
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", default="")
    parser.add_argument(
        "--existing-json",
        default="",
        help="JSON existant à compléter, jamais un chemin de fichier.",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Lit le mot de passe sur l'entrée standard.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    username = args.username.strip().casefold()
    if not username:
        raise ValueError("L'identifiant est requis")
    if args.password_stdin:
        password = sys.stdin.read().rstrip("\r\n")
    else:
        password = getpass.getpass("Mot de passe du bêta-testeur : ")

    existing: dict[str, object] = {}
    if args.existing_json.strip():
        users = parse_beta_users(args.existing_json)
        existing = {
            key: {
                "display_name": user.display_name,
                "password_hash": user.password_hash,
            }
            for key, user in users.items()
        }
    existing[username] = {
        "display_name": args.display_name.strip() or username,
        "password_hash": hash_password(password),
    }
    print(json.dumps(existing, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
