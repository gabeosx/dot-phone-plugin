"""Generate synthetic speech fixtures with macOS's local voice; no provider request."""

import argparse
import subprocess
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    args = parser.parse_args()
    directory = Path(args.directory)
    assets = directory / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(Path(__file__).with_name("baseline.yaml").read_text())
    for turn in data["scenarios"][0]["turns"]:
        output = directory / turn["audio"]
        subprocess.run(
            [
                "say",
                "--file-format=WAVE",
                "--data-format=LEI16@24000",
                "-o",
                str(output),
                turn["user"],
            ],
            check=True,
        )
    (directory / "baseline.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    print("Three synthetic speech recordings generated locally.")


if __name__ == "__main__":
    main()
