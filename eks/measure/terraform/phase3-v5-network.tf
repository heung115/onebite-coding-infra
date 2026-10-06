locals {
  phase3_peer_http_rule_enabled = var.enable_phase3_spot_interruption && (
    endswith(var.phase3_spot_experiment_tag, "-v5") || endswith(var.phase3_spot_experiment_tag, "-v6") || endswith(var.phase3_spot_experiment_tag, "-v7") || endswith(var.phase3_spot_experiment_tag, "-v8")
  )
}

resource "aws_vpc_security_group_ingress_rule" "phase3_node_peer_http" {
  count = local.phase3_peer_http_rule_enabled ? 1 : 0

  security_group_id            = module.eks.node_security_group_id
  referenced_security_group_id = module.eks.node_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
  description                  = "Phase 3 system-to-Spot Pod HTTP probe"

  tags = merge(local.tags, {
    Name                 = "${var.cluster_name}-phase3-node-peer-http"
    "measure-experiment" = local.phase3_spot_tag
  })
}
