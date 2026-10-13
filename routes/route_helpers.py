def dispatch_legacy(**_route_parameters):
    from .bridge import dispatch_legacy as dispatch

    return dispatch()


def register_legacy_routes(blueprint, routes):
    for rule, methods in routes:
        blueprint.add_url_rule(
            rule,
            view_func=dispatch_legacy,
            methods=methods,
            provide_automatic_options=False,
        )
