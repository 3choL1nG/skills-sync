#!/usr/bin/env python3
"""演示脚本。"""
import argparse


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="world")
    args = parser.parse_args()
    print(f"hello, {args.name}")


if __name__ == "__main__":
    main()
