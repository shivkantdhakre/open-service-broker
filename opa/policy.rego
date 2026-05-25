package broker.authz

import future.keywords.in

# By default, deny all requests
default allow = false

# Whitelisted actions
allowed_actions := {
    "create_route",
    "update_route",
    "delete_route",
    "configure_load_balancing",
    "update_rate_limit",
    "create_cluster",
    "update_cluster",
    "scale_service",
    "configure_circuit_breaker",
    "configure_retry_policy",
    "configure_timeout"
}

# Main authorization rule
allow {
    count(errors) == 0
}

# Rule 1: Action must be whitelisted
errors["Action is not allowed by policy."] {
    not allowed_actions[input.action]
}

# Rule 2: Ensure 'target_cluster' or 'weighted_clusters' is present when creating routes
errors["Route action requires target_cluster or weighted_clusters."] {
    input.action == "create_route"
    not input.parameters.target_cluster
    not input.parameters.weighted_clusters
}

# Rule 3: Enforce maximum blast radius score for non-forced changes
errors["Blast radius risk score exceeds allowed threshold (0.60)."] {
    not input.force
    input.blast_radius.risk_score > 0.60
}

# Rule 4: Prevent deletions in production environment unless forced
errors["Route deletion in production is prohibited unless forced."] {
    input.action == "delete_route"
    input.context.environment == "production"
    not input.force
}

# Rule 5: Wildcard route restrictions (prefix "/" must not target arbitrary clusters without headers or query parameters matching)
errors["Wildcard route matching '/' without header or query parameter constraints is unsafe."] {
    action_affects_route
    input.parameters.prefix == "/"
    not input.parameters.headers
    not input.parameters.query_parameters
    not input.force
}

# Rule 6: Public ingress limits (ensure route prefixes do not match sensitive paths or restrict them)
errors["Public ingress to sensitive path '/admin' is prohibited."] {
    action_affects_route
    input.parameters.prefix == "/admin"
    not input.force
}

# Helper rule to identify route creation or update
action_affects_route {
    input.action == "create_route"
}
action_affects_route {
    input.action == "update_route"
}

# Output format details for the client
errors_list = [msg | errors[msg]]
