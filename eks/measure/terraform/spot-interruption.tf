locals {
  # Batch-specific so the FIS template cannot match a worker from an older run.
  phase3_spot_tag = var.phase3_spot_experiment_tag

  phase3_interruption_event_patterns = {
    spot-interruption = {
      source        = ["aws.ec2"]
      "detail-type" = ["EC2 Spot Instance Interruption Warning"]
    }
    rebalance-recommendation = {
      source        = ["aws.ec2"]
      "detail-type" = ["EC2 Instance Rebalance Recommendation"]
    }
    instance-state-change = {
      source        = ["aws.ec2"]
      "detail-type" = ["EC2 Instance State-change Notification"]
      detail = {
        state = ["stopping", "stopped", "terminated"]
      }
    }
    ec2-scheduled-change = {
      source        = ["aws.health"]
      "detail-type" = ["AWS Health Event"]
      detail = {
        service           = ["EC2"]
        eventTypeCategory = ["scheduledChange"]
      }
    }
  }

  phase3_interruption_events = {
    for name, pattern in local.phase3_interruption_event_patterns :
    name => pattern if var.enable_phase3_spot_interruption
  }

  phase3_interruption_event_targets = {
    for pair in setproduct(keys(local.phase3_interruption_events), ["karpenter", "audit"]) :
    "${pair[0]}-${pair[1]}" => pair
  }
}

resource "aws_sqs_queue" "karpenter_interruption" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  name                       = "${var.cluster_name}-karpenter-interruption"
  message_retention_seconds  = 300
  receive_wait_time_seconds  = 10
  visibility_timeout_seconds = 30
  sqs_managed_sse_enabled    = true
  tags                       = merge(local.tags, { Name = "${var.cluster_name}-karpenter-interruption" })
}

resource "aws_sqs_queue" "phase3_event_audit" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  name                      = "${var.cluster_name}-phase3-event-audit"
  message_retention_seconds = 86400
  receive_wait_time_seconds = 1
  sqs_managed_sse_enabled   = true
  tags                      = merge(local.tags, { Name = "${var.cluster_name}-phase3-event-audit" })
}

resource "aws_cloudwatch_event_rule" "karpenter_interruption" {
  for_each = local.phase3_interruption_events

  name           = "${var.cluster_name}-${each.key}"
  description    = "Route Karpenter involuntary interruption and rebalance events."
  event_bus_name = "default"
  event_pattern  = jsonencode(each.value)
  state          = "ENABLED"
  tags           = merge(local.tags, { Name = "${var.cluster_name}-${each.key}" })
}

resource "aws_cloudwatch_event_target" "interruption_queues" {
  for_each = local.phase3_interruption_event_targets

  event_bus_name = "default"
  rule           = aws_cloudwatch_event_rule.karpenter_interruption[each.value[0]].name
  target_id      = "${var.cluster_name}-${each.key}"
  arn            = each.value[1] == "karpenter" ? aws_sqs_queue.karpenter_interruption[0].arn : aws_sqs_queue.phase3_event_audit[0].arn
  depends_on = [
    aws_sqs_queue_policy.karpenter_interruption,
    aws_sqs_queue_policy.phase3_event_audit
  ]
}

data "aws_iam_policy_document" "eventbridge_to_karpenter_queue" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  statement {
    sid       = "AllowPhase3EventBridgeRules"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.karpenter_interruption[0].arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [for rule in values(aws_cloudwatch_event_rule.karpenter_interruption) : rule.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

data "aws_iam_policy_document" "eventbridge_to_audit_queue" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  statement {
    sid       = "AllowPhase3EventBridgeRules"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.phase3_event_audit[0].arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [for rule in values(aws_cloudwatch_event_rule.karpenter_interruption) : rule.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sqs_queue_policy" "karpenter_interruption" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  queue_url = aws_sqs_queue.karpenter_interruption[0].url
  policy    = data.aws_iam_policy_document.eventbridge_to_karpenter_queue[0].json
}

resource "aws_sqs_queue_policy" "phase3_event_audit" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  queue_url = aws_sqs_queue.phase3_event_audit[0].url
  policy    = data.aws_iam_policy_document.eventbridge_to_audit_queue[0].json
}

data "aws_iam_policy_document" "karpenter_interruption_queue" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  statement {
    sid = "ConsumeKarpenterInterruptionQueue"
    actions = [
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ReceiveMessage"
    ]
    resources = [aws_sqs_queue.karpenter_interruption[0].arn]
  }
}

resource "aws_iam_role_policy" "karpenter_interruption_queue" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  name   = "${var.cluster_name}-karpenter-interruption-queue"
  role   = aws_iam_role.karpenter_controller.name
  policy = data.aws_iam_policy_document.karpenter_interruption_queue[0].json
}

data "aws_iam_policy_document" "fis_spot_trust" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["fis.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:fis:${var.region}:${data.aws_caller_identity.current.account_id}:experiment/*"]
    }
  }
}

resource "aws_iam_role" "fis_spot_interruption" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  name               = "${var.cluster_name}-phase3-fis-spot"
  assume_role_policy = data.aws_iam_policy_document.fis_spot_trust[0].json
  tags               = merge(local.tags, { Name = "${var.cluster_name}-phase3-fis-spot" })
}

data "aws_iam_policy_document" "fis_spot_interruption" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  statement {
    sid       = "DescribeSpotTargetInstances"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]
  }

  statement {
    sid       = "InterruptOnlyTaggedPhase3SpotInstances"
    actions   = ["ec2:SendSpotInstanceInterruptions"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = ["onebite"]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Temporary"
      values   = ["true"]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/measure-experiment"
      values   = [local.phase3_spot_tag]
    }
  }
}

resource "aws_iam_role_policy" "fis_spot_interruption" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  name   = "${var.cluster_name}-phase3-fis-spot"
  role   = aws_iam_role.fis_spot_interruption[0].name
  policy = data.aws_iam_policy_document.fis_spot_interruption[0].json
}

resource "aws_fis_experiment_template" "phase3_spot_interruption" {
  count = var.enable_phase3_spot_interruption ? 1 : 0

  description = "Phase 3: interrupt exactly one running, tagged Karpenter Spot worker and observe recovery."
  role_arn    = aws_iam_role.fis_spot_interruption[0].arn
  tags        = merge(local.tags, { Name = "${var.cluster_name}-phase3-spot-interruption" })

  action {
    name      = "interrupt-one-phase3-spot-worker"
    action_id = "aws:ec2:send-spot-instance-interruptions"

    parameter {
      key   = "durationBeforeInterruption"
      value = "PT2M"
    }

    target {
      key   = "SpotInstances"
      value = "phase3-spot-worker"
    }
  }

  stop_condition {
    source = "none"
  }

  target {
    name           = "phase3-spot-worker"
    resource_type  = "aws:ec2:spot-instance"
    selection_mode = "COUNT(1)"

    resource_tag {
      key   = "Project"
      value = "onebite"
    }

    resource_tag {
      key   = "Temporary"
      value = "true"
    }

    resource_tag {
      key   = "measure-experiment"
      value = local.phase3_spot_tag
    }
  }

  depends_on = [
    aws_iam_role_policy.fis_spot_interruption,
    aws_sqs_queue_policy.karpenter_interruption,
    aws_cloudwatch_event_target.interruption_queues
  ]
}
