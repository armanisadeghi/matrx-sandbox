# Local Agent Tasks (Cowork Mode)

Tasks that the Cowork agent can handle via browser automation, file editing,
and local tooling. These do NOT require human intervention beyond initial
AWS Console login.

---

## Status

| # | Task | Status | Depends On |
|---|------|--------|------------|
| 1 | Create S3 Bucket (browser) | DONE | — |
| 2 | Create EC2 Key Pair (browser) | DONE | — |
| 3 | Create ECR Repository (browser) | DONE | — |
| 4 | Code Review & Quality Audit | DONE | Full report at `docs/CODE_REVIEW.md` |
| 5 | Verify Terraform Configuration | DONE | Included in code review report |
| 6 | Review & Test docker-compose.yml | DONE | Included in code review report |
| 7 | Documentation Improvements | TODO | — |

---

## 1. Create S3 Bucket (Browser) — DONE

Created `matrx-sandbox-storage-prod-2024` in us-east-1 with ACLs disabled,
public access blocked, versioning enabled, SSE-S3 + Bucket Key.

```
MATRX_S3_BUCKET=matrx-sandbox-storage-prod-2024
MATRX_S3_REGION=us-east-1
```

---

## 2. Create EC2 Key Pair (Browser) — DONE

Created `matrx-sandbox-key` (RSA, .pem). File secured at `~/Code/secrets/matrx-sandbox-key.pem` (chmod 400).

```
EC2_KEY_PAIR_NAME=matrx-sandbox-key
EC2_KEY_FILE=~/Code/secrets/matrx-sandbox-key.pem
```

---

## 3. Create ECR Repository (Browser) — DONE

Created private repo `matrx-sandbox` with AES-256 encryption, mutable tags.

```
ECR_REPO_URI=872515272894.dkr.ecr.us-east-1.amazonaws.com/matrx-sandbox
```

---

## 4. Code Review & Quality Audit

**No dependencies**. Can start immediately.

Review all Python source files for:
- Type hint completeness
- Error handling gaps
- Security issues (hardcoded secrets, missing input validation)
- Missing docstrings
- Consistency with project conventions
- Test coverage gaps

Files to review:
- `orchestrator/orchestrator/*.py`
- `orchestrator/tests/*.py`
- `sandbox-image/sdk/matrx_agent/*.py`
- `sandbox-image/scripts/*.sh`

---

## 5. Verify Terraform Configuration

**No dependencies**. Can start immediately.

- Validate all `.tf` files for correctness
- Check for security best practices (security group rules, IAM least privilege)
- Verify variable defaults make sense
- Confirm `terraform.tfvars.example` matches actual variables
- Check for missing outputs that would be useful

---

## 6. Review & Test docker-compose.yml

**No dependencies**. Can start immediately.

- Verify docker-compose.yml is valid and complete
- Check that LocalStack config matches what the orchestrator expects
- Verify volume mounts and environment variables
- Ensure healthcheck configurations are correct

---

## 7. Documentation Improvements

**No dependencies**. Can start immediately.

- Cross-reference README.md with actual code
- Verify ARCHITECTURE.md accuracy
- Check that API endpoint docs match actual routes
- Add any missing configuration documentation
