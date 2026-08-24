# E2B

!!! note "E2B Environment class"

    - [Read on GitHub](https://github.com/swe-agent/mini-swe-agent/blob/main/src/minisweagent/environments/extra/e2b.py)
    - Requires an [E2B](https://e2b.dev) API key

    ??? note "Full source code"

        ```python
        --8<-- "src/minisweagent/environments/extra/e2b.py"
        ```

::: minisweagent.environments.extra.e2b

This environment executes commands in [E2B](https://e2b.dev) cloud sandboxes. No local
Docker daemon is needed, which makes it a good fit for large-scale SWE-bench runs from
a laptop or a small CI box.

The first time a Docker image is used it is converted into a persistent E2B template via
`Template.build()`. Subsequent runs reuse the cached template, so the one-time build cost
is paid once per unique image *and* resource spec.

## Setup

1. Install the dependencies:
   ```bash
   pip install "mini-swe-agent[e2b]"
   ```

2. Set your API key:
   ```bash
   export E2B_API_KEY="your-e2b-api-key"
   ```

## Usage

```
mini-extra swebench \
    --subset verified \
    --split test \
    --workers 50 \
    --environment-class e2b
```

Or in a YAML config:

```yaml
environment:
  environment_class: e2b
  cwd: /testbed
  sandbox_timeout: 3600
  cpu_count: 2
  memory_mb: 8192
```

SWE-bench evaluation images are large; the 2048 MiB default is often not enough, so raise
`memory_mb`. Changing `cpu_count` or `memory_mb` changes the template identity, so the
next run rebuilds the template rather than silently reusing the old resource spec.

## Other E2B-compatible control planes

The E2B SDK reads the `E2B_DOMAIN` environment variable and derives the API URL as
`https://api.$E2B_DOMAIN`. Pointing it at a compatible control plane is therefore all it
takes — no code or config change:

```bash
export E2B_DOMAIN="your-e2b-compatible-domain"
```

{% include-markdown "../../_footer.md" %}
