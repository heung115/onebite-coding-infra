#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"
SESSION_DIR="${SESSION_DIR:-$ROOT/measure/results/$(date -u +%Y%m%dT%H%M%SZ)}"
OUTPUT_FILE="${ENVIRONMENT_OUTPUT:-$SESSION_DIR/environment.json}"
CONFIGURED_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"

if [[ "$CONFIGURED_REGION" != "$REGION" ]]; then
  printf 'Refusing capture: configured region is %s, expected %s.\n' "${CONFIGURED_REGION:-unset}" "$REGION" >&2
  exit 2
fi
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ "${ACCOUNT_ID: -4}" != "0497" ]]; then
  printf 'Refusing capture: caller account does not end in 0497.\n' >&2
  exit 3
fi
if [[ ! -f "$KUBECONFIG_PATH" ]]; then
  printf 'Dedicated kubeconfig is missing: %s\n' "$KUBECONFIG_PATH" >&2
  exit 4
fi
mkdir -p "$SESSION_DIR"

CLUSTER_JSON="$(aws eks describe-cluster --region "$REGION" --name "$CLUSTER" --query 'cluster.{name:name,status:status,version:version,platformVersion:platformVersion,createdAt:createdAt,endpointHost:endpoint,resourcesVpcConfig:resourcesVpcConfig}' --output json)"
NODEGROUP_SYSTEM="$(aws eks describe-nodegroup --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "${CLUSTER}-system" --query 'nodegroup.{status:status,version:version,amiType:amiType,instanceTypes:instanceTypes,scalingConfig:scalingConfig,subnets:subnets}' --output json)"
NODEGROUP_EXPERIMENT="$(aws eks describe-nodegroup --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "${CLUSTER}-experiment" --query 'nodegroup.{status:status,version:version,amiType:amiType,releaseVersion:releaseVersion,instanceTypes:instanceTypes,scalingConfig:scalingConfig,subnets:subnets}' --output json)"
EXPERIMENT_AMI_ID="$(terraform -chdir="$ROOT/measure/terraform" output -raw experiment_ami_id)"
EXPERIMENT_AZ="$(terraform -chdir="$ROOT/measure/terraform" output -raw experiment_availability_zone)"
ADDONS="$(aws eks list-addons --region "$REGION" --cluster-name "$CLUSTER" --query addons --output json | jq -c '.')"
ADDON_DETAILS='[]'
for ADDON in $(printf '%s' "$ADDONS" | jq -r '.[]'); do
  DETAIL="$(aws eks describe-addon --region "$REGION" --cluster-name "$CLUSTER" --addon-name "$ADDON" --query 'addon.{name:addonName,status:status,version:addonVersion,configuration:configurationValues}' --output json)"
  ADDON_DETAILS="$(jq -c --argjson item "$DETAIL" '. + [$item]' <<<"$ADDON_DETAILS")"
done

KUBERNETES_NODES="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -o json | jq '[.items[] | {name:.metadata.name,labels:{pool:.metadata.labels["measure-pool"],zone:.metadata.labels["topology.kubernetes.io/zone"],instanceType:.metadata.labels["node.kubernetes.io/instance-type"],nodegroup:.metadata.labels["eks.amazonaws.com/nodegroup"]},createdAt:.metadata.creationTimestamp,ready:[.status.conditions[]|select(.type=="Ready")|.status][0],capacityPods:.status.capacity.pods,allocatablePods:.status.allocatable.pods,capacityCPU:.status.capacity.cpu,allocatableCPU:.status.allocatable.cpu,capacityMemory:.status.capacity.memory,allocatableMemory:.status.allocatable.memory}]')"
EC2_INSTANCES="$(aws ec2 describe-instances --region "$REGION" --filters Name=tag:Project,Values=onebite Name=tag:Temporary,Values=true Name=instance-state-name,Values=pending,running,stopping,stopped --query 'Reservations[].Instances[].{instanceId:InstanceId,state:State.Name,type:InstanceType,imageId:ImageId,zone:Placement.AvailabilityZone,launchTime:LaunchTime,networkInterfaces:NetworkInterfaces[].{id:NetworkInterfaceId,status:Status,privateIp:PrivateIpAddress,privateIps:PrivateIpAddresses[].PrivateIpAddress,ipv4Prefixes:Ipv4Prefixes[].Ipv4Prefix,publicIp:Association.PublicIp},tags:Tags[?Key==`Project` || Key==`Temporary` || Key==`eks:nodegroup-name` || Key==`Name`]}' --output json)"
POD_NETWORK_STATE="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get pods --all-namespaces -o json | jq '[.items[] | {namespace:.metadata.namespace,name:.metadata.name,node:.spec.nodeName,phase:.status.phase,podIP:.status.podIP,podIPs:[.status.podIPs[]?.ip]}]')"
KUBERNETES_VERSION="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" version -o json | jq -c '{client:.clientVersion.gitVersion,server:.serverVersion.gitVersion}')"

jq -n \
  --arg capturedAtUTC "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg accountSuffix "0497" \
  --arg region "$REGION" \
  --arg experimentAmiId "$EXPERIMENT_AMI_ID" \
  --arg experimentAvailabilityZone "$EXPERIMENT_AZ" \
  --arg awsCliVersion "$(aws --version 2>&1)" \
  --arg terraformVersion "$(terraform version -json | jq -r '.terraform_version')" \
  --arg kubectlClientVersion "$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" version --client=true --output=json | jq -r '.clientVersion.gitVersion')" \
  --argjson cluster "$CLUSTER_JSON" \
  --argjson systemNodeGroup "$NODEGROUP_SYSTEM" \
  --argjson experimentNodeGroup "$NODEGROUP_EXPERIMENT" \
  --argjson addons "$ADDON_DETAILS" \
  --argjson nodes "$KUBERNETES_NODES" \
  --argjson instances "$EC2_INSTANCES" \
  --argjson podNetworkState "$POD_NETWORK_STATE" \
  --argjson kubernetesVersion "$KUBERNETES_VERSION" \
  '{capturedAtUTC:$capturedAtUTC,accountSuffix:$accountSuffix,region:$region,experimentAmiId:$experimentAmiId,experimentAvailabilityZone:$experimentAvailabilityZone,awsCliVersion:$awsCliVersion,terraformVersion:$terraformVersion,kubernetesVersion:$kubernetesVersion,cluster:$cluster,nodeGroups:{system:$systemNodeGroup,experiment:$experimentNodeGroup},addons:$addons,nodes:$nodes,instances:$instances,podNetworkState:$podNetworkState}' \
  > "$OUTPUT_FILE"

printf 'Environment saved to %s\n' "$OUTPUT_FILE"
