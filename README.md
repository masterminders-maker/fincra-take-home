# Fincra DevOps Take-Home

Infrastructure-as-Code, GitOps, and CI/CD for a Flask web app running on
AWS EKS Fargate behind an Application Load Balancer. Built to the brief in
`DevOps_take-home.pdf`.

---

## What this repo contains

| Path | Purpose |
|---|---|
| `app/` | The Flask app, its `Dockerfile`, and local `docker-compose.yml` |
| `infra/` | AWS CDK (Python) — VPC, security groups, EKS Fargate cluster, Argo CD |
| `k8s/base/` | Kubernetes manifests: `Deployment`, `Service`, `Ingress` |
| `k8s/overlays/dev/` | Kustomize overlay for the dev environment |
| `argocd/application.yaml` | Argo CD `Application` (mirror of the one CDK installs) |
| `.github/workflows/config.yml` | GitHub Actions pipeline |
| `docs/architecture.md` | Architecture diagram and flow description |

---

## Architecture at a glance

```
Developer                                      AWS account
  │                                              │
  │  git push main                               │
  ▼                                              │
GitHub Actions ─────────────────────────────────▶│
  1. lint + test (app smoke, cdk synth,         │
     kustomize build)                           │
  2. docker build → push to ECR (tag = sha)      │   ECR repo: fincra-app
  3. cdk deploy (idempotent)                    │   ├── VPC + SGs
  4. kustomize edit set image + git push         │   └── EKS Fargate cluster
                                                 │         ├── AWS LB Controller
     (from here on, pull-based)                  │         └── Argo CD
                                                 │                │
                                      Argo CD ◀──┼────────────────┘
                                      watches repo
                                      reconciles manifests
                                                 │
                                      Deployment → Service → Ingress
                                                         │
                                                         ▼
                                              Application Load Balancer
                                                         │
                                                         ▼
                                              https://<alb-dns>/
                                              → "Hello, from Fincra!"
```

Full diagram in [`docs/architecture.md`](docs/architecture.md).

---

## Prerequisites

- AWS account with permissions to create VPC, EKS, IAM, ECR resources
- An IAM role with a trust policy that allows GitHub's OIDC provider to assume
  it from this repo (see [Setting up OIDC](#setting-up-oidc))
- Python 3.11+, Node.js 20+, Docker, `kubectl`, `kustomize`, and `aws-cli v2`
  installed locally if you want to deploy or iterate outside CI

---

## Quick start

### 1. Clone and configure

```bash
git clone https://github.com/YOUR_GITHUB_USER/fincra-devops-takehome.git
cd fincra-devops-takehome
```

Replace `YOUR_GITHUB_USER` in:

- `k8s/overlays/dev/kustomization.yaml`
- `argocd/application.yaml`
- `infra/stacks/eks_stack.py` (`DEFAULT_REPO_URL`)

### 2. Set GitHub repo variables

In your repo settings → Secrets and variables → Actions → Variables:

| Name | Value |
|---|---|
| `AWS_ACCOUNT_ID` | Your 12-digit account ID |
| `AWS_DEPLOY_ROLE_ARN` | ARN of the IAM role GH Actions assumes via OIDC |

No secrets required — OIDC handles auth.

### 3. Run the app locally

```bash
cd app
make up_log
# in another shell
curl http://localhost
# → Hello, from Fincra!
```

### 4. Deploy to AWS

Push to `main`. GitHub Actions handles everything:

```
push main
  → lint-and-test
  → build-and-push (ECR)
  → deploy-infra  (cdk deploy)
  → bump-image    (commits kustomize tag → Argo CD syncs)
```

First run on an empty account takes ~20 minutes (most of that is the EKS
cluster). Subsequent runs are image build + CDK diff + kustomize bump —
typically under 5 minutes.

### 5. Access the app

After the first successful deploy:

```bash
aws eks update-kubeconfig --name fincra-cluster --region us-east-1 \
  --role-arn $(aws cloudformation describe-stacks \
    --stack-name FincraEksStack \
    --query "Stacks[0].Outputs[?OutputKey=='ClusterAdminRoleArn'].OutputValue" \
    --output text)

kubectl get ingress fincra-app -n default
# copy the ADDRESS column into a browser → "Hello, from Fincra!"
```

---

## Setting up OIDC

GitHub Actions → AWS without static keys:

1. Create an IAM OIDC identity provider for `token.actions.githubusercontent.com`.
2. Create an IAM role with a trust policy limited to
   `repo:YOUR_GITHUB_USER/fincra-devops-takehome:ref:refs/heads/main`.
3. Attach a policy allowing CDK, ECR, and EKS actions the pipeline needs.
4. Put the role ARN in the `AWS_DEPLOY_ROLE_ARN` repo variable.

For a take-home, `PowerUserAccess` + the ability to pass IAM roles is fine.
In production it would be a narrowly scoped custom policy.

---

## Design decisions

**Why CDK Python, not TypeScript or Terraform.** The app is Python, the CDK is
Python, the Kubernetes admission tooling (if we add it later) is Python — one
language across the stack reduces context-switch tax for reviewers. Terraform
would have worked; the brief stated CDK as the preference.

**Why two stacks, not one.** Network and cluster have very different blast
radii. Splitting them means a VPC tweak doesn't force a plan against a
thousand-resource EKS stack, and vice versa.

**Why Fargate, not managed node groups.** The brief names Fargate explicitly.
No node groups to patch, no autoscaler to tune — the cluster scales at the pod
level. Trade-off: a few things (DaemonSets, privileged containers, GPU pods)
aren't available on Fargate, but none of those apply here.

**Why Argo CD, given GitHub Actions already deploys.** The intro names
GitOps + Argo CD + Kustomize as the house style. CI handles infra and image
promotion because those *must* be imperative (an AWS account isn't in git).
Argo CD handles the app layer because that's where declarative drift-detection
and self-heal pay off.

**Why the image bump is a git commit, not a direct `kubectl set image`.**
Direct `set image` puts the cluster and git out of sync — exactly what Argo
CD was installed to prevent. Commit → Argo CD syncs → cluster matches HEAD.
Rollback is just `git revert`.

**Why the ALB Controller over the classic AWS cloud provider's LoadBalancer.**
On Fargate, `type: LoadBalancer` Services create Classic ELBs, which aren't
what anyone wants in 2026. The ALB Controller gives path-based routing, WAF
integration, and ACM certs — same cost, far more capability.

**Why two Fargate profiles for `kube-system`-adjacent namespaces.** Every pod
on Fargate needs a matching profile selector. The ALB Controller runs in
`kube-system`; Argo CD runs in its own namespace. One profile per namespace
keeps the selector logic obvious.

---

## Assumptions

- **Region:** `us-east-1`. Change `AWS_REGION` in `.github/workflows/config.yml`
  and the default in `infra/app.py` if you need another.
- **Kubernetes version:** 1.30. Bump `KubernetesVersion.V1_30` and the
  `kubectl_v30` layer together.
- **TLS:** not configured. The brief allows HTTP on port 80. Adding HTTPS is
  one annotation on the Ingress (`alb.ingress.kubernetes.io/certificate-arn`)
  plus an ACM cert — deliberately out of scope here.
- **Argo CD auth:** default admin password, ClusterIP-only service. Access
  via `kubectl port-forward svc/argocd-server -n argocd 8080:443`. In prod,
  SSO integration and an Ingress would be added.
- **Image scanning:** `scanOnPush=true` is enabled on the ECR repo. Findings
  are not yet gating the pipeline — that would be the next hardening step.
- **State:** no app state, no database. Adding RDS would live in a new stack
  with its own security group and an explicit rule from `cluster_sg`.

---

## Rolling back

Three levels, from fastest to deepest:

1. **App rollback:** `git revert` the `bump-image` commit on `main`. Argo CD
   reconciles within seconds.
2. **Manifest rollback:** `git revert` the change to `k8s/overlays/dev/` or
   `k8s/base/`. Same Argo CD path.
3. **Infra rollback:** `git revert` the CDK change, push. The `deploy-infra`
   job runs `cdk deploy` which brings the stack back to the previous state.
   For destructive changes CDK will flag them and `--require-approval never`
   means they go through — so destructive infra changes should land through a
   PR with a human review.

---

## What I'd add next (out of scope for 1 day)

- **Unit tests** for the CDK stacks using `aws-cdk.assertions`
- **Policy-as-code** gate — `cdk-nag` in the lint job, failing on high severity
- **SBOM + image signing** — `syft` + `cosign` in `build-and-push`
- **Prod overlay** + branch-based promotion (`main` → dev, tags → prod)
- **Observability** — CloudWatch Container Insights, or the Prometheus stack
  behind Grafana, wired to the same Argo CD
- **Secrets** — External Secrets Operator pointing at AWS Secrets Manager

---

## Time spent

Roughly 6 hours across scaffolding, CDK, manifests, the workflow, and this
README. The single biggest time sink was the IRSA plumbing for the ALB
Controller — worth doing properly because getting it wrong shows up as an
Ingress that sits in `ADDRESS: <pending>` forever with no obvious error.
