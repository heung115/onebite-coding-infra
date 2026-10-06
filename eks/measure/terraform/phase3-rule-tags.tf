locals {
  phase3_node_sg_rule_tag_indexes = var.enable_phase3_spot_interruption ? toset([
    for index in range(11) : tostring(index)
  ]) : toset([])

  phase3_node_peer_http_rule_id = local.phase3_peer_http_rule_enabled ? aws_vpc_security_group_ingress_rule.phase3_node_peer_http[0].security_group_rule_id : ""
  phase3_node_sg_rule_ids_to_tag = var.enable_phase3_spot_interruption ? [
    for rule_id in data.aws_vpc_security_group_rules.phase3_node_sg[0].ids : rule_id
    if rule_id != local.phase3_node_peer_http_rule_id
  ] : []
}

data "aws_vpc_security_group_rules" "phase3_cluster_sg" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  filter {
    name   = "group-id"
    values = [module.eks.cluster_security_group_id]
  }

  depends_on = [module.eks]
}

data "aws_vpc_security_group_rules" "phase3_node_sg" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  filter {
    name   = "group-id"
    values = [module.eks.node_security_group_id]
  }

  depends_on = [module.eks]
}

resource "aws_ec2_tag" "phase3_cluster_sg_rule_project" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  resource_id = one(data.aws_vpc_security_group_rules.phase3_cluster_sg[0].ids)
  key         = "Project"
  value       = local.tags.Project
}

resource "aws_ec2_tag" "phase3_cluster_sg_rule_temporary" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  resource_id = one(data.aws_vpc_security_group_rules.phase3_cluster_sg[0].ids)
  key         = "Temporary"
  value       = local.tags.Temporary
}

resource "aws_ec2_tag" "phase3_node_sg_rule_project" {
  for_each = local.phase3_node_sg_rule_tag_indexes

  resource_id = local.phase3_node_sg_rule_ids_to_tag[tonumber(each.key)]
  key         = "Project"
  value       = local.tags.Project

  lifecycle {
    precondition {
      condition     = length(local.phase3_node_sg_rule_ids_to_tag) == 11
      error_message = "Expected exactly 11 module-owned node security-group rules after excluding the separately tagged Phase 3 peer rule."
    }
  }
}

resource "aws_ec2_tag" "phase3_node_sg_rule_temporary" {
  for_each = local.phase3_node_sg_rule_tag_indexes

  resource_id = local.phase3_node_sg_rule_ids_to_tag[tonumber(each.key)]
  key         = "Temporary"
  value       = local.tags.Temporary

  lifecycle {
    precondition {
      condition     = length(local.phase3_node_sg_rule_ids_to_tag) == 11
      error_message = "Expected exactly 11 module-owned node security-group rules after excluding the separately tagged Phase 3 peer rule."
    }
  }
}
