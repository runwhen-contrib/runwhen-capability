import helper


def main(ctx, since="30m", limit=3, reason=None):
    return {
        "summary": {
            "namespace": ctx.resource.namespace,
            "name": ctx.resource.name,
            "path": ctx.resource.path,
            "threshold": ctx.settings.latency_threshold_ms,
            "since": since,
            "limit": limit,
            "label": ctx.settings.label,
            "hosts": list(ctx.settings.hosts),
            "dry": ctx.dry_run,
            "lib": helper.MARKER,
        }
    }
