#!/bin/bash
# aws_bench.sh — Orchestrate benchmark runs on AWS instances.
#
# Provisions ephemeral instances, deploys code, tunes system,
# runs benchmarks, collects results, and tears down infrastructure.
#
# Usage:
#   ./aws_bench.sh --instance NAME       # Run on one instance
#   ./aws_bench.sh --all                 # Run on all instances
#   ./aws_bench.sh --dry-run             # Print commands, don't execute
#   ./aws_bench.sh --list                # List available instances
#
# Prerequisites: aws CLI configured, SSH key permissions, jq installed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF_FILE="$SCRIPT_DIR/instances.conf"
RESULTS_DIR="$SCRIPT_DIR/results"
PROJECT_TAG="isocurves-bench"
TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=30"

DRY_RUN=0
PREBUILT=0
CATALOG=0
COMPETITORS=0
FLAGSHIP_ONLY=0
CAT_DOMAIN=""
CAT_ID=""
CAT_LEAN=0
LANDSCAPE=0
FW_PROFILE=""          # --framework preflight|reference (isobench framework; implies Ubuntu infra)
DATA_DIR=""
DEADMAN_MIN="${DEADMAN_MIN:-300}"
TARGETS=()
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

# --- Cleanup state ---
INSTANCE_ID=""
SG_ID=""
KEY_NAME=""
KEY_FILE=""

# --- Cost tracking ---
COST_LOG=""
TOTAL_COST=0

# ---- Argument parsing ----

usage() {
    echo "Usage: $0 [--instance NAME] [--all] [--dry-run] [--prebuilt]"
    echo "          [--catalog] [--competitors] [--flagship-only] [--list] [--region REGION]"
    echo ""
    echo "  --catalog        Run the testcase catalog (bench/testcases/catalog.tsv)"
    echo "                   instead of the legacy synthetic-field suite."
    echo "  --competitors    With --catalog, also run the matplotlib/ContourPy/GDAL"
    echo "                   head-to-head (installs a Python+GDAL stack on the instance)."
    echo "  --flagship-only  With --catalog, restrict to flagship testcases."
    echo "  --framework P    isobench framework run, P = preflight | reference. reference is"
    echo "                   refused unless bench/framework/gate.py finds a PASSing preflight"
    echo "                   of the current source tree + spec. Requires --data DIR."
    echo "  --landscape      Competitive landscape suite (bench/competitors/landscape):"
    echo "                   Ubuntu 24.04 node, pinned competitors built on the node,"
    echo "                   in-process head-to-heads + real data + 3D + GIS file-to-file."
    echo "                   Requires --data DIR (dataset bundle with MANIFEST.sha256)."
    echo "                   Results: bench/results/landscape/<instance>/<timestamp>/"
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --instance)
            shift; TARGETS+=("$1") ;;
        --all)
            while IFS= read -r line; do
                [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
                TARGETS+=("$(echo "$line" | awk '{print $1}')")
            done < "$CONF_FILE"
            ;;
        --dry-run)
            DRY_RUN=1 ;;
        --prebuilt)
            PREBUILT=1 ;;
        --catalog)
            CATALOG=1 ;;
        --competitors)
            COMPETITORS=1 ;;
        --flagship-only)
            FLAGSHIP_ONLY=1 ;;
        --domain)
            shift; CAT_DOMAIN="$1" ;;
        --id)
            shift; CAT_ID="$1" ;;
        --lean)
            CAT_LEAN=1 ;;
        --landscape)
            LANDSCAPE=1 ;;
        --framework)
            shift; FW_PROFILE="$1"; LANDSCAPE=1 ;;
        --data)
            shift; DATA_DIR="$1" ;;
        --list)
            echo "Available instances:"
            while IFS= read -r line; do
                [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
                name=$(echo "$line" | awk '{print $1}')
                type=$(echo "$line" | awk '{print $2}')
                arch=$(echo "$line" | awk '{print $3}')
                gpu=$(echo "$line" | awk '{print $4}')
                cost=$(echo "$line" | awk '{print $5}')
                gpu_label="CPU"
                [ "$gpu" = "1" ] && gpu_label="GPU"
                printf "  %-18s %-20s %-8s %-4s ~\$%s/hr\n" "$name" "$type" "$arch" "$gpu_label" "$cost"
            done < "$CONF_FILE"
            exit 0
            ;;
        --region)
            shift; REGION="$1" ;;
        *)
            usage ;;
    esac
    shift
done

# Only the isobench framework mode is maintained. The catalog / landscape /
# plain modes drove the pre-library tools, now in archive/ (archive/README.md).
if [ -z "$FW_PROFILE" ]; then
    echo "aws_bench.sh: only --framework preflight|reference is supported; the legacy modes"
    echo "were archived with their tools (archive/README.md; tag pre-library-layout)."
    exit 2
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "ERROR: specify --instance NAME or --all" >&2
    usage
fi

# ---- Helpers ----

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  DRY-RUN: $*"
        return 0
    fi
    "$@"
}

get_instance_conf() {
    local name="$1"
    grep -E "^${name}\s" "$CONF_FILE" | head -1
}

# Fetch on-demand price from AWS Pricing API (us-east-1 endpoint only)
get_instance_price() {
    local inst_type="$1"
    local price
    price=$(aws pricing get-products --region us-east-1 \
        --service-code AmazonEC2 \
        --filters \
            "Type=TERM_MATCH,Field=instanceType,Value=${inst_type}" \
            "Type=TERM_MATCH,Field=operatingSystem,Value=Linux" \
            "Type=TERM_MATCH,Field=tenancy,Value=Shared" \
            "Type=TERM_MATCH,Field=preInstalledSw,Value=NA" \
            "Type=TERM_MATCH,Field=capacitystatus,Value=Used" \
        --query 'PriceList[0]' --output text 2>/dev/null \
        | jq -r '
            .terms.OnDemand
            | to_entries[0].value.priceDimensions
            | to_entries[0].value.pricePerUnit.USD' 2>/dev/null)
    if [ -n "$price" ] && [ "$price" != "null" ]; then
        echo "$price"
    else
        echo ""
    fi
}

# Compute cost from start epoch, end epoch, and $/hr rate
compute_cost() {
    local start_epoch="$1" end_epoch="$2" rate="$3"
    local elapsed=$(( end_epoch - start_epoch ))
    # EC2 per-second billing, 60-second minimum
    [ "$elapsed" -lt 60 ] && elapsed=60
    awk "BEGIN { printf \"%.4f\", ($rate / 3600.0) * $elapsed }"
}

lookup_ami() {
    local arch="$1" gpu="${2:-0}"
    local ami_arch="x86_64"
    [ "$arch" = "arm64" ] && ami_arch="arm64"

    if [ "$LANDSCAPE" -eq 1 ]; then
        # R13: the image the stored results were measured on (its kernel is part
        # of the machine key), while it is still available
        local pinned
        pinned=$(python3 "$SCRIPT_DIR/framework/isobench/store.py" ami-pin "$ami_arch" 2>/dev/null || true)
        if [ -n "$pinned" ] && [ "$(aws ec2 describe-images --region "$REGION" --image-ids "$pinned" \
                --query 'Images[0].State' --output text 2>/dev/null)" = "available" ]; then
            echo "$pinned"
            return 0
        fi
        # Landscape suite: Ubuntu 24.04 LTS (Canonical) — has PROJ/GEOS/SQLite
        # dev packages for building GDAL from source, unlike AL2023.
        local uarch="amd64"; [ "$arch" = "arm64" ] && uarch="arm64"
        aws ec2 describe-images --region "$REGION" --owners 099720109477 \
            --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-${uarch}-server-*" \
                      "Name=state,Values=available" \
            --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text
        return
    fi

    if [ "$gpu" -eq 1 ]; then
        # GPU instances use the AWS Deep Learning Base GPU AMI, which ships the
        # NVIDIA driver + CUDA toolkit preinstalled (at /usr/local/cuda). This
        # avoids the fragile on-instance driver/CUDA install on the plain AL2023
        # AMI, which has no driver and whose CUDA package versions drift.
        local name_filter="Deep Learning Base OSS Nvidia Driver GPU AMI (Amazon Linux 2023)*"
        [ "$arch" = "arm64" ] && \
            name_filter="Deep Learning ARM64 Base OSS Nvidia Driver GPU AMI (Amazon Linux 2023)*"
        aws ec2 describe-images \
            --region "$REGION" --owners amazon \
            --filters "Name=name,Values=${name_filter}" \
                      "Name=state,Values=available" \
            --query 'reverse(sort_by(Images, &CreationDate))[0].ImageId' \
            --output text
        return
    fi

    aws ec2 describe-images \
        --region "$REGION" \
        --owners amazon \
        --filters \
            "Name=name,Values=al2023-ami-2023.*-kernel-*-${ami_arch}" \
            "Name=state,Values=available" \
        --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
        --output text
}

wait_ssh() {
    local host="$1" key="$2" user="$3"
    log "Waiting for SSH on $host..."
    for i in $(seq 1 60); do
        if ssh $SSH_OPTS -i "$key" "${user}@${host}" "echo ok" 2>/dev/null | grep -q ok; then
            log "SSH ready"
            return 0
        fi
        sleep 5
    done
    log "ERROR: SSH timeout after 5 minutes"
    return 1
}

# ---- Cleanup (trap handler) ----

cleanup() {
    local exit_code=$?
    log "Cleanup triggered (exit=$exit_code)"

    if [ -n "$INSTANCE_ID" ]; then
        log "Terminating instance $INSTANCE_ID..."
        run aws ec2 terminate-instances --region "$REGION" \
            --instance-ids "$INSTANCE_ID" >/dev/null 2>&1 || true

        if [ "$DRY_RUN" -eq 0 ] && [ -n "$INSTANCE_ID" ]; then
            log "Waiting for instance termination..."
            aws ec2 wait instance-terminated --region "$REGION" \
                --instance-ids "$INSTANCE_ID" 2>/dev/null || true
        fi
    fi

    if [ -n "$SG_ID" ]; then
        log "Deleting security group $SG_ID..."
        # Retry a few times (SG deletion can lag behind instance termination)
        for i in 1 2 3 4 5; do
            if run aws ec2 delete-security-group --region "$REGION" \
                    --group-id "$SG_ID" 2>/dev/null; then
                break
            fi
            sleep 10
        done
    fi

    if [ -n "$KEY_NAME" ]; then
        log "Deleting key pair $KEY_NAME..."
        run aws ec2 delete-key-pair --region "$REGION" \
            --key-name "$KEY_NAME" 2>/dev/null || true
    fi

    if [ -n "$KEY_FILE" ] && [ -f "$KEY_FILE" ]; then
        rm -f "$KEY_FILE"
    fi

    if [ $exit_code -ne 0 ]; then
        log "Script failed with exit code $exit_code"
    fi
}

# ---- Main benchmark function ----

run_benchmark() {
    local name="$1"
    local conf
    conf=$(get_instance_conf "$name")
    if [ -z "$conf" ]; then
        log "ERROR: instance '$name' not found in $CONF_FILE"
        return 1
    fi

    local inst_type arch gpu cost
    inst_type=$(echo "$conf" | awk '{print $2}')
    arch=$(echo "$conf" | awk '{print $3}')
    gpu=$(echo "$conf" | awk '{print $4}')
    cost=$(echo "$conf" | awk '{print $5}')

    # Look up live pricing (falls back to instances.conf estimate)
    local live_price=""
    if [ "$DRY_RUN" -eq 0 ]; then
        live_price=$(get_instance_price "$inst_type")
    fi
    local rate="${live_price:-$cost}"

    log "========================================"
    log "Instance: $name ($inst_type, $arch, GPU=$gpu)"
    if [ -n "$live_price" ]; then
        log "Rate: \$$live_price/hr (live pricing)"
    else
        log "Rate: ~\$$cost/hr (from instances.conf)"
    fi
    log "========================================"

    # Reset cleanup state
    INSTANCE_ID=""
    SG_ID=""
    KEY_NAME=""
    KEY_FILE=""
    LAUNCH_EPOCH=""
    trap cleanup EXIT INT TERM

    local out_dir="$RESULTS_DIR/$name/$TIMESTAMP"
    [ "$LANDSCAPE" -eq 1 ] && out_dir="$RESULTS_DIR/landscape/$name/$TIMESTAMP"
    [ -n "$FW_PROFILE" ] && out_dir="$RESULTS_DIR/framework/$FW_PROFILE/$name/$TIMESTAMP"
    local fw_preflight="" fw_bundle_sha=""
    if [ -n "$FW_PROFILE" ]; then
        case "$FW_PROFILE" in preflight|reference) ;; *) log "ERROR: --framework preflight|reference"; return 1 ;; esac
        if [ "$FW_PROFILE" = "reference" ]; then
            # The pre-flight rule: no bare-metal run without a PASSing preflight of THIS tree + spec.
            fw_preflight=$(python3 "$SCRIPT_DIR/framework/gate.py" --instance-type "$inst_type" --require-validation) \
                || { log "ERROR: pre-flight gate closed"; return 1; }
            fw_bundle_sha=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['artifact_manifest']['bundle_sha256'])" \
                "$fw_preflight/run.json")
            log "Pre-flight gate open: $fw_preflight (artifact bundle ${fw_bundle_sha:0:12})"
        fi
    fi
    if [ "$LANDSCAPE" -eq 1 ] && { [ -z "$DATA_DIR" ] || [ ! -f "$DATA_DIR/MANIFEST.sha256" ]; }; then
        log "ERROR: --landscape needs --data DIR containing MANIFEST.sha256"
        return 1
    fi
    mkdir -p "$out_dir"

    # ---- 1. PROVISION ----
    log "--- Provisioning ---"

    # Key pair
    KEY_NAME="${PROJECT_TAG}-${name}-${TIMESTAMP}"
    KEY_FILE="$(mktemp /tmp/${KEY_NAME}.XXXXXX.pem)"
    log "Creating key pair: $KEY_NAME"
    if [ "$DRY_RUN" -eq 0 ]; then
        aws ec2 create-key-pair --region "$REGION" \
            --key-name "$KEY_NAME" \
            --query 'KeyMaterial' --output text > "$KEY_FILE"
        chmod 600 "$KEY_FILE"
    fi

    # Security group (SSH from caller's IP)
    local my_ip
    my_ip=$(curl -s https://checkip.amazonaws.com)
    SG_NAME="${PROJECT_TAG}-${name}-${TIMESTAMP}"
    log "Creating security group: $SG_NAME (SSH from $my_ip)"
    if [ "$DRY_RUN" -eq 0 ]; then
        SG_ID=$(aws ec2 create-security-group --region "$REGION" \
            --group-name "$SG_NAME" \
            --description "Benchmark SSH access for $name" \
            --query 'GroupId' --output text)
        aws ec2 authorize-security-group-ingress --region "$REGION" \
            --group-id "$SG_ID" \
            --protocol tcp --port 22 \
            --cidr "${my_ip}/32" >/dev/null
    else
        SG_ID="sg-dry-run"
    fi

    # AMI lookup — GPU instances get the Deep Learning GPU AMI (driver+CUDA)
    if [ "$gpu" -eq 1 ]; then
        log "Looking up Deep Learning Base GPU AMI ($arch, driver+CUDA preinstalled)..."
    else
        log "Looking up latest Amazon Linux 2023 AMI ($arch)..."
    fi
    local ami_id
    if [ "$DRY_RUN" -eq 0 ]; then
        ami_id=$(lookup_ami "$arch" "$gpu")
        if [ -z "$ami_id" ] || [ "$ami_id" = "None" ]; then
            log "ERROR: no matching AMI found (arch=$arch gpu=$gpu)"
            return 1
        fi
        log "AMI: $ami_id"
    else
        ami_id="ami-dry-run"
    fi

    # Launch instance
    log "Launching $inst_type..."
    local extra_launch=()
    if [ "$LANDSCAPE" -eq 1 ]; then
        # Bigger root disk (GDAL builds + venv + fields) and a dead-man switch:
        # the node powers itself off after DEADMAN_MIN minutes, and power-off
        # means terminate — it cannot outlive a crashed orchestrator.
        extra_launch=(--instance-initiated-shutdown-behavior terminate
            --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=60,VolumeType=gp3,DeleteOnTermination=true}")
    fi
    if [ "$DRY_RUN" -eq 0 ]; then
        INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
            --image-id "$ami_id" \
            --instance-type "$inst_type" \
            --key-name "$KEY_NAME" \
            --security-group-ids "$SG_ID" \
            "${extra_launch[@]}" \
            --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${PROJECT_TAG}-${name}},{Key=Project,Value=${PROJECT_TAG}}]" \
            --query 'Instances[0].InstanceId' --output text)
        log "Instance: $INSTANCE_ID"
        LAUNCH_EPOCH=$(date +%s)

        log "Waiting for instance to be running..."
        aws ec2 wait instance-running --region "$REGION" \
            --instance-ids "$INSTANCE_ID"

        local public_ip
        public_ip=$(aws ec2 describe-instances --region "$REGION" \
            --instance-ids "$INSTANCE_ID" \
            --query 'Reservations[0].Instances[0].PublicIpAddress' \
            --output text)
        log "Public IP: $public_ip"
    else
        INSTANCE_ID="i-dry-run"
        public_ip="1.2.3.4"
        log "DRY-RUN: Would launch $inst_type"
    fi

    local ssh_user="ec2-user"
    [ "$LANDSCAPE" -eq 1 ] && ssh_user="ubuntu"
    local SSH="ssh $SSH_OPTS -i $KEY_FILE ${ssh_user}@${public_ip}"
    local SCP="scp $SSH_OPTS -i $KEY_FILE"
    local RSYNC="rsync -az --progress -e 'ssh $SSH_OPTS -i $KEY_FILE'"

    # Wait for SSH
    if [ "$DRY_RUN" -eq 0 ]; then
        wait_ssh "$public_ip" "$KEY_FILE" "$ssh_user"
    fi

    # ---- 2. DEPLOY ----
    if [ "$LANDSCAPE" -eq 1 ]; then
        log "--- Arming dead-man switch (${DEADMAN_MIN} min) ---"
        run $SSH "sudo shutdown -h +${DEADMAN_MIN} >/dev/null 2>&1 || true"
      if [ -n "$FW_PROFILE" ]; then
        log "--- Deploying framework source tree (exact file list + provenance) ---"
        local flist pfile
        flist=$(mktemp); pfile=$(mktemp)
        python3 "$SCRIPT_DIR/framework/isobench/provenance.py" list > "$flist"
        python3 - "$pfile" "$FW_PROFILE" "$inst_type" "$SCRIPT_DIR/framework" <<'PYEOF'
import json, sys, time
sys.path.insert(0, sys.argv[4])
from isobench import provenance
json.dump({"source_tree_hash": provenance.tree_hash(), "git": provenance.git_state(),
           "profile": sys.argv[2], "instance_type": sys.argv[3],
           "launched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, open(sys.argv[1], "w"), indent=1)
PYEOF
        run eval "$RSYNC --files-from='$flist' '$SCRIPT_DIR/../' '${ssh_user}@${public_ip}:~/isocurves/'"
        run $SCP "$pfile" "${ssh_user}@${public_ip}:~/provenance.json"
        cp "$pfile" "$out_dir/launch_provenance.json"
        log "--- Uploading dataset bundle ---"
        run eval "$RSYNC '$DATA_DIR/' '${ssh_user}@${public_ip}:~/landscape_data/'"
        local fw_store="$RESULTS_DIR/framework/store"
        if [ -d "$fw_store/timing" ]; then
            # R13: results already measured (same code, data, method, machine) are reused
            log "--- Uploading the result store ---"
            run eval "$RSYNC --exclude='competitor_bundles/' '$fw_store/' '${ssh_user}@${public_ip}:~/store/'"
        fi
        log "--- Provisioning node (framework) ---"
        if [ "$FW_PROFILE" = "reference" ]; then
            # R8 build-once: install the preflight's artifact bundle unchanged; build nothing
            run $SCP "$fw_preflight/artifacts.tar.gz" "${ssh_user}@${public_ip}:~/artifacts.tar.gz"
            run $SSH "ARTIFACTS=~/artifacts.tar.gz EXPECT_BUNDLE_SHA=$fw_bundle_sha bash ~/isocurves/bench/framework/cloud/setup_ubuntu.sh"
        else
            local cbundle
            cbundle=$(python3 "$SCRIPT_DIR/framework/isobench/store.py" competitor-bundle)
            if [ -n "$cbundle" ]; then
                # R13: competitor builds reused byte for byte (same pins and worker sources)
                log "Competitor builds: stored bundle $(basename "$cbundle" | cut -c1-12) (not rebuilt)"
                run $SCP "$cbundle" "${ssh_user}@${public_ip}:~/competitors.tar.gz"
                run $SSH "COMPETITORS=~/competitors.tar.gz EXPECT_COMPETITORS_SHA=$(shasum -a 256 "$cbundle" | cut -d' ' -f1) bash ~/isocurves/bench/framework/cloud/setup_ubuntu.sh"
            else
                run $SSH "bash ~/isocurves/bench/framework/cloud/setup_ubuntu.sh"
            fi
        fi
      else
        log "--- Deploying code (landscape) ---"
        run eval "$RSYNC \
            --filter='- **/target/' --filter='- **/node_modules/' --filter='- __pycache__/' \
            --filter='- *.o' --filter='- *.so' --filter='- *.dylib' --filter='- *.metallib' \
            --filter='- *.air' --filter='- *.dSYM/' --filter='- bench/results/' \
            --filter='- python/build/' --filter='- python/.venv/' \
            --filter='+ iso2d/*.c' --filter='+ iso2d/*.h' --filter='+ iso2d/Makefile' \
            --filter='+ iso2d/**/*.c' --filter='+ iso2d/**/*.h' \
            --filter='+ iso2d/demos/isosweep-contour/Makefile' \
            --filter='+ iso2d/' --filter='+ iso2d/**/' \
            --filter='+ iso3d/*.c' --filter='+ iso3d/*.h' --filter='+ iso3d/Makefile' \
            --filter='+ iso3d/**/*.c' --filter='+ iso3d/**/*.h' \
            --filter='+ iso3d/' --filter='+ iso3d/**/' \
            --filter='+ common/*.c' --filter='+ common/*.h' --filter='+ common/' \
            --filter='+ python/setup.py' --filter='+ python/pyproject.toml' \
            --filter='+ python/isocurves/*.py' --filter='+ python/isocurves/*.c' \
            --filter='+ python/' --filter='+ python/isocurves/' \
            --filter='+ bench/*.sh' --filter='+ bench/testcases/' --filter='+ bench/testcases/**' \
            --filter='+ bench/competitors/' --filter='+ bench/competitors/landscape/' \
            --filter='+ bench/competitors/landscape/**' --filter='+ bench/' \
            --filter='+ Makefile' --filter='- *' \
            '$SCRIPT_DIR/../' '${ssh_user}@${public_ip}:~/isocurves/'"
        log "--- Uploading dataset bundle ---"
        run eval "$RSYNC '$DATA_DIR/' '${ssh_user}@${public_ip}:~/landscape_data/'"
        log "--- Provisioning node (pinned toolchains, GDAL builds) ---"
        run $SSH "bash ~/isocurves/bench/competitors/landscape/cloud/setup_ubuntu.sh"
      fi
    elif [ "$PREBUILT" -eq 1 ] && [ "$gpu" -eq 0 ]; then
        # Pre-built binary deploy — skip compiler install, just copy binaries + scripts
        local dist_dir="$SCRIPT_DIR/dist/$arch"
        if [ ! -f "$dist_dir/bench" ] || [ ! -f "$dist_dir/bench3d" ]; then
            log "ERROR: Pre-built binaries not found in $dist_dir"
            log "Run: ./bench/build_linux.sh --arch $arch"
            return 1
        fi

        log "--- Deploying pre-built binaries ---"
        run $SSH "mkdir -p ~/isocurves/iso2d ~/isocurves/iso3d ~/isocurves/bench"
        run $SCP "$dist_dir/bench" "${ssh_user}@${public_ip}:~/isocurves/iso2d/bench"
        run $SCP "$dist_dir/bench3d" "${ssh_user}@${public_ip}:~/isocurves/iso3d/bench3d"
        run $SCP "$SCRIPT_DIR/run_bench.sh" "$SCRIPT_DIR/tune_cpu.sh" \
            "${ssh_user}@${public_ip}:~/isocurves/bench/"
        # Catalog assets (needed for --catalog runs)
        if [ "$CATALOG" -eq 1 ]; then
            run $SCP "$SCRIPT_DIR/run_catalog.sh" "$SCRIPT_DIR/run_catalog_suite.sh" \
                "${ssh_user}@${public_ip}:~/isocurves/bench/"
            run $SCP -r "$SCRIPT_DIR/testcases" "$SCRIPT_DIR/competitors" \
                "${ssh_user}@${public_ip}:~/isocurves/bench/"
        fi
        run $SSH "chmod +x ~/isocurves/iso2d/bench ~/isocurves/iso3d/bench3d ~/isocurves/bench/*.sh"
    else
        if [ "$PREBUILT" -eq 1 ] && [ "$gpu" -eq 1 ]; then
            log "NOTE: --prebuilt ignored for GPU instances (CUDA must link on-instance)"
        fi

        log "--- Deploying code ---"
        # Only sync source files needed for building and benchmarking
        run eval "$RSYNC \
            --filter='- *.o' --filter='- *.metallib' --filter='- *.air' \
            --filter='- bench/results/' --filter='- *.dSYM/' \
            --filter='+ iso2d/*.c' --filter='+ iso2d/*.h' \
            --filter='+ iso2d/**/*.c' --filter='+ iso2d/**/*.h' \
            --filter='+ iso2d/**/*.cu' --filter='+ iso2d/**/*.metal' \
            --filter='+ iso2d/**/*.sh' --filter='+ iso2d/Makefile' \
            --filter='+ iso2d/gempak_bench/Makefile' \
            --filter='+ iso2d/' --filter='+ iso2d/**/' \
            --filter='+ iso3d/*.c' --filter='+ iso3d/*.h' \
            --filter='+ iso3d/**/*.c' --filter='+ iso3d/**/*.h' \
            --filter='+ iso3d/**/*.cu' --filter='+ iso3d/**/*.metal' \
            --filter='+ iso3d/**/*.py' --filter='+ iso3d/Makefile' \
            --filter='+ iso3d/' --filter='+ iso3d/**/' \
            --filter='+ common/*.c' --filter='+ common/*.h' \
            --filter='+ common/' \
            --filter='+ bench/*.sh' --filter='+ bench/*.conf' \
            --filter='+ bench/testcases/' --filter='+ bench/testcases/**' \
            --filter='+ bench/competitors/' --filter='+ bench/competitors/**' \
            --filter='+ bench/' \
            --filter='+ Makefile' \
            --filter='- *' \
            '$SCRIPT_DIR/../' '${ssh_user}@${public_ip}:~/isocurves/'"

        # Install dependencies
        log "Installing build dependencies..."
        run $SSH "sudo dnf install -y gcc make 2>/dev/null | tail -3"

        # GPU instances run on the Deep Learning Base GPU AMI, which already has
        # the NVIDIA driver + CUDA toolkit at /usr/local/cuda. Just confirm nvcc
        # is reachable (it is not on a non-login shell's default PATH).
        local mkenv=""
        if [ "$gpu" -eq 1 ]; then
            mkenv="PATH=/usr/local/cuda/bin:\$PATH"
            log "Verifying preinstalled CUDA toolkit..."
            run $SSH "$mkenv nvcc --version 2>&1 | tail -2 || echo 'WARNING: nvcc not found on GPU AMI'"
        fi

        # Build. Carry the CUDA bin dir on PATH for the GPU build so make's
        # `which nvcc` detection selects the CUDA backend (otherwise it would
        # silently fall back to a CPU-only build).
        log "Building..."
        run $SSH "cd ~/isocurves/iso2d && make clean && $mkenv make bench -j\$(nproc) 2>&1 | tail -8"
        run $SSH "cd ~/isocurves/iso3d && make clean && $mkenv make bench3d -j\$(nproc) 2>&1 | tail -8"
    fi

    # ---- 3. TUNE ----
    log "--- Tuning system ---"
    run $SSH "sudo bash ~/isocurves/bench/tune_cpu.sh"

    # ---- 4. BENCHMARK ----
    log "--- Running benchmarks ---"
    local node_rc=0
    if [ -n "$FW_PROFILE" ]; then
        # a failed or aborted run is still collected (its run.json records why), then fails
        run $SSH "bash ~/isocurves/bench/framework/cloud/run_node.sh $FW_PROFILE" || node_rc=$?
        if [ "$node_rc" -ne 0 ]; then
            log "Node run failed (exit $node_rc); collecting what it recorded"
            mkdir -p "$out_dir"
            $RSYNC "${ssh_user}@${public_ip}:~/fw_results/" "$out_dir/" || true
            return "$node_rc"
        fi
    elif [ "$LANDSCAPE" -eq 1 ]; then
        local ref="no" env_desc
        if [[ "$inst_type" == *metal* ]]; then
            ref="yes"
            env_desc="AWS EC2 ${inst_type}, dedicated bare-metal server (no hypervisor, no co-tenants), Ubuntu 24.04, turbo disabled, performance governor, THP off"
        else
            env_desc="AWS EC2 ${inst_type} (virtualized smoke test, NOT a reference environment)"
        fi
        run $SSH "SMOKE=${SMOKE:-0} RUNS=${RUNS:-7} bash ~/isocurves/bench/competitors/landscape/cloud/run_all.sh '$env_desc' '$inst_type' '$ref'"
    elif [ "$CATALOG" -eq 1 ]; then
        # Testcase catalog: C algorithm modes (+ optional competitor head-to-head)
        local cat_args="~/bench_results"
        # GPU bench links libcudart; the CUDA runtime dir is not on the default
        # loader path, so carry it on LD_LIBRARY_PATH for the run.
        local runenv=""
        if [ "$gpu" -eq 1 ]; then
            cat_args="$cat_args --gpu"
            runenv="LD_LIBRARY_PATH=/usr/local/cuda/lib64:\$LD_LIBRARY_PATH"
        fi
        [ "$FLAGSHIP_ONLY" -eq 1 ] && cat_args="$cat_args --flagship-only"
        [ -n "$CAT_DOMAIN" ] && cat_args="$cat_args --domain $CAT_DOMAIN"
        [ -n "$CAT_ID" ] && cat_args="$cat_args --id $CAT_ID"
        [ "$CAT_LEAN" -eq 1 ] && cat_args="$cat_args --lean"

        if [ "$COMPETITORS" -eq 1 ]; then
            log "Installing competitor stack (python venv: matplotlib + ContourPy)..."
            # AL2023 has no gdal in core repos and its system python rejects bare
            # pip installs, so use an isolated venv for the pure-pip scientific
            # stack. gdal_contour is benchmarked separately on the reference
            # workstation against the identical dumped fields (its slowness is
            # algorithmic, not hardware-dependent).
            run $SSH "
                sudo dnf install -y python3 2>/dev/null | tail -1 || true
                python3 -m venv ~/compvenv
                ~/compvenv/bin/pip install --quiet --upgrade pip 2>&1 | tail -1 || true
                ~/compvenv/bin/pip install --quiet numpy matplotlib contourpy 2>&1 | tail -2 || true
                echo 'competitor stack probe:'
                ~/compvenv/bin/python -c 'import numpy,matplotlib,contourpy; print(\"  py libs OK:\", numpy.__version__, matplotlib.__version__, contourpy.__version__)' 2>&1 | tail -1
            "
            cat_args="$cat_args --competitors"
            run $SSH "$runenv COMPETITOR_PYTHON=~/compvenv/bin/python bash ~/isocurves/bench/run_catalog_suite.sh $cat_args"
        else
            run $SSH "$runenv bash ~/isocurves/bench/run_catalog_suite.sh $cat_args"
        fi
    else
        # Legacy synthetic-field suite
        local bench_args="~/bench_results"
        if [ "$PREBUILT" -eq 1 ] && [ "$gpu" -eq 0 ]; then
            bench_args="$bench_args --skip-build"
        fi
        if [ "$gpu" -eq 1 ]; then
            bench_args="$bench_args --gpu"
        fi
        run $SSH "bash ~/isocurves/bench/run_bench.sh $bench_args"
    fi

    # ---- 5. COLLECT ----
    log "--- Collecting results ---"
    if [ -n "$FW_PROFILE" ]; then
        run eval "$RSYNC '${ssh_user}@${public_ip}:~/fw_results/' '$out_dir/'"
        if [ "$FW_PROFILE" = "preflight" ]; then
            # the bundle the reference run will install unchanged (R8)
            run eval "$RSYNC '${ssh_user}@${public_ip}:~/artifacts.tar.gz' '$out_dir/'"
            # and its competitor part, for later preflights (R13)
            [ "$DRY_RUN" -eq 0 ] && python3 "$SCRIPT_DIR/framework/isobench/store.py" competitor-bundle-from \
                "$out_dir/artifacts.tar.gz" || true
        fi
        if [ "$FW_PROFILE" = "reference" ] && [ "$DRY_RUN" -eq 0 ]; then
            log "--- Local attestation (incl. C13 preflight link) ---"
            python3 "$SCRIPT_DIR/framework/audit/conformance.py" "$out_dir" --preflight "$fw_preflight" || true
            log "--- Result store: admitting this run's records (R13) ---"
            python3 "$SCRIPT_DIR/framework/store_admit.py" "$out_dir" --preflight "$fw_preflight" \
                --data-dir "$DATA_DIR" || true
            python3 "$SCRIPT_DIR/framework/isobench/store.py" ami-pin "$( [ "$arch" = arm64 ] && echo arm64 || echo x86_64)" "$ami_id" >/dev/null
        fi
    elif [ "$LANDSCAPE" -eq 1 ]; then
        run eval "$RSYNC '${ssh_user}@${public_ip}:~/bench_results/landscape/' '$out_dir/'"
    else
        run eval "$RSYNC '${ssh_user}@${public_ip}:~/bench_results/' '$out_dir/'"
    fi

    log "Results saved to: $out_dir"

    # ---- 6. TEARDOWN + COST ----
    log "--- Teardown ---"
    local terminate_epoch
    terminate_epoch=$(date +%s)

    if [ "$DRY_RUN" -eq 0 ] && [ -n "$LAUNCH_EPOCH" ]; then
        local run_cost
        run_cost=$(compute_cost "$LAUNCH_EPOCH" "$terminate_epoch" "$rate")
        local elapsed=$(( terminate_epoch - LAUNCH_EPOCH ))
        local mins=$(( elapsed / 60 ))
        local secs=$(( elapsed % 60 ))
        log "Instance uptime: ${mins}m ${secs}s"
        log "Instance cost:   \$$run_cost (@ \$$rate/hr)"
        COST_LOG="${COST_LOG}${name}\t${inst_type}\t${mins}m${secs}s\t\$${run_cost}\t\$${rate}/hr\n"
        TOTAL_COST=$(awk "BEGIN { printf \"%.4f\", $TOTAL_COST + $run_cost }")

        # Save cost info alongside results
        {
            echo "instance: $name"
            echo "type: $inst_type"
            echo "rate_per_hour: $rate"
            echo "launch_epoch: $LAUNCH_EPOCH"
            echo "terminate_epoch: $terminate_epoch"
            echo "elapsed_seconds: $elapsed"
            echo "cost_usd: $run_cost"
        } > "$out_dir/cost.txt"
    fi

    # Explicitly trigger cleanup for this instance
    cleanup
    # Reset trap state for next instance
    INSTANCE_ID=""
    SG_ID=""
    KEY_NAME=""
    KEY_FILE=""
    LAUNCH_EPOCH=""
    trap - EXIT INT TERM
}

# ---- Execute benchmarks ----

log "Isocurves AWS Benchmark Automation"
log "Region: $REGION"
log "Targets: ${TARGETS[*]}"
log "Timestamp: $TIMESTAMP"
if [ "$DRY_RUN" -eq 1 ]; then
    log "*** DRY RUN MODE ***"
fi
echo ""

for target in "${TARGETS[@]}"; do
    run_benchmark "$target"
    echo ""
done

log "All benchmarks complete."
log "Results in: $RESULTS_DIR"

# --- Cost summary ---
if [ -n "$COST_LOG" ]; then
    echo ""
    log "========================================"
    log "COST SUMMARY"
    log "========================================"
    printf "  %-18s %-20s %-10s %-10s %s\n" "NAME" "TYPE" "UPTIME" "COST" "RATE"
    printf "  %-18s %-20s %-10s %-10s %s\n" "----" "----" "------" "----" "----"
    echo -e "$COST_LOG" | while IFS=$'\t' read -r n t u c r; do
        [ -z "$n" ] && continue
        printf "  %-18s %-20s %-10s %-10s %s\n" "$n" "$t" "$u" "$c" "$r"
    done
    echo "  ----------------------------------------"
    printf "  TOTAL: \$%s\n" "$TOTAL_COST"
    log "========================================"
fi

# Verify no lingering resources
if [ "$DRY_RUN" -eq 0 ]; then
    log "Checking for lingering resources..."
    lingering=$(aws ec2 describe-instances --region "$REGION" \
        --filters "Name=tag:Project,Values=${PROJECT_TAG}" \
                  "Name=instance-state-name,Values=running,pending,stopping,stopped" \
        --query 'Reservations[*].Instances[*].InstanceId' --output text 2>/dev/null || true)
    if [ -n "$lingering" ] && [ "$lingering" != "None" ]; then
        log "WARNING: Found lingering instances: $lingering"
    else
        log "No lingering instances found. Clean."
    fi
fi
