.PHONY: demo

# Record-ready Companion Portal walkthrough — see demo/README.md.
demo:
	cd demo && uv sync && uv run playwright install chromium && uv run companion-demo
