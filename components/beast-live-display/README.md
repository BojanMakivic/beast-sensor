# beast-live-display

Persistent WebSocket Plotly display for Beast Sensor

## Installation instructions

```sh
uv pip install beast-live-display
```

### Development install (editable)

When developing this component locally, install it in editable mode so Streamlit picks up code changes without rebuilding a wheel. Run this from the directory that contains `pyproject.toml`:

```sh
uv pip install -e . --force-reinstall
```

## Usage instructions

```python
from beast_live_display import beast_live_display

beast_live_display(
    source_mode="latest",
    recording_path=None,
    exercise="bench",
    history_seconds=90,
    paused=False,
)
```

## Build a wheel

To package this component for distribution:

1. Build the frontend assets (from `beast_live_display/frontend`):

   ```sh
   npm.cmd install
   npm.cmd run build
   ```

2. Build the Python wheel using UV (from the project root):
   ```sh
   uv build
   ```

This will create a `dist/` directory containing your wheel. The wheel includes the compiled frontend from `beast_live_display/frontend/build`.

### Requirements

- Python >= 3.10
- Node.js >= 22.12

### Expected output

- `dist/beast_live_display-0.0.1-py3-none-any.whl`
- If you run `uv run --with build python -m build` (without `--wheel`), you’ll also get an sdist: `dist/beast-live-display-0.0.1.tar.gz`
