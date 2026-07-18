from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "webapp.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.getenv("PORT", os.getenv("INFOFIN_WEB_PORT", "8080"))),
        workers=1,
    )


if __name__ == "__main__":
    main()
