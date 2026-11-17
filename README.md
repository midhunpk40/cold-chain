# Cloud-Based Cold-Chain Medicine Excursion Detection and Audit Platform (AWS)

A serverless prototype that watches temperature and humidity from simulated cold-chain sensors, detects
excursions and silent sensors, notifies a team, runs an acknowledge / escalate / disposition workflow, exposes a
role-based API, and keeps audit evidence. All infrastructure is defined in Terraform.

> **Prototype only.** This is a course project. It is not a validated pharmaceutical (GxP / 21 CFR Part 11) system.

- **Region:** `ap-south-1` (Mumbai)
- **Infrastructure as code:** Terraform (AWS provider ~> 6.0)
- **Runtime:** Python 3.12 (Lambda), Python 3.11 (laptop tools)
- **Front end:** none (backend and API only)

---

## 1. What it does

| Capability              | How                                                                                                                                   |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Secure ingestion        | Simulated sensors publish over MQTT with mutual TLS (X.509 certificates) to AWS IoT Core                                              |
| Validation              | Every message is checked for schema, plausibility, timestamp, and device identity. Bad messages go to a quarantine prefix, never lost |
| Dedupe and ordering     | Conditional writes make repeat delivery safe. Late and out-of-order readings are stored but never change device state                 |
| Excursion detection     | Count-based: N consecutive bad readings open an excursion, M consecutive good readings recover it. Limits are per product             |
| Silent-sensor detection | A scheduled watchdog opens a `TELEMETRY_MISSING` record when a monitored device stops reporting                                       |
| Notification            | EventBridge starts a Step Functions workflow that emails the team through SNS                                                         |
| Human workflow          | Acknowledge, escalate if unacknowledged, then record a disposition (RELEASE, QUARANTINE, DESTROY)                                     |
| Role-based API          | Cognito login (JWT), API Gateway authorizer, three roles                                                                              |
| Audit evidence          | JSON evidence per excursion in S3, API access logs, CloudTrail with log file validation, a hash-based evidence report                 |
| Monitoring              | CloudWatch alarms on Lambda errors, workflow failures, and API 5xx, sent to a separate SNS topic                                      |

---

## 2. Architecture

```
sensor (simulator.py, mTLS certificate)
   -> AWS IoT Core  (rule: coldchain_telemetry)
   -> ingest Lambda  (validate, dedupe, store, detect)
        -> DynamoDB (readings, sensor-state, excursions)  + S3 (raw/, rejected/, evidence/)
        -> if an excursion opens: EventBridge bus "coldchain-events"
             -> Step Functions workflow -> SNS email (alerts / escalations)

user -> Cognito login (JWT) -> API Gateway (JWT authorizer) -> API Lambda -> actions Lambda
        -> DynamoDB / S3 (acknowledge, disposition, evidence)

EventBridge schedule (rate 1 minute, disabled by default) -> watchdog Lambda
        -> gap record (type TELEMETRY_MISSING) -> same bus -> same workflow

CloudWatch alarms -> SNS topic coldchain-ops-alarms -> email
CloudTrail + API Gateway access log -> audit
```

### AWS services used

IoT Core, Lambda, DynamoDB, S3, EventBridge, Step Functions, SNS, API Gateway (HTTP API), Cognito, CloudWatch
(logs and alarms), IAM, CloudTrail, AWS Budgets.

KMS is **not** deployed (see [Known limits](#9-known-limits)).

---

## 3. Repository layout

```
cold-chain/
  terraform/
    versions.tf  variables.tf  budget.tf  s3.tf  iot.tf  lambda.tf   # base, IoT, ingest Lambda
    dynamodb.tf  events.tf  workflow.tf  actions.tf                    # data, bus, workflow, actions
    cognito.tf   api.tf     watchdog.tf  alarms.tf    cloudtrail.tf    # auth, API, watchdog, monitoring
    outputs.tf
  lambdas/
    ingest/handler.py      # validate, dedupe, detect, open/close excursions and gaps
    actions/handler.py     # acknowledge and disposition rules
    api/handler.py         # HTTP API: routing, roles, response shaping
    watchdog/handler.py    # silent-sensor detection
  simulator/
    simulator.py           # mTLS sensor simulator (certificates in simulator/certs, not committed)
  tests/
    send_cases.py          # ingest validation (17 checks)
    detect_cases.py        # excursion detection (18 checks)
    api.py                 # role-based API (19 checks) and manual API calls
    gap_cases.py           # watchdog (16 checks)
    act.py  show.py  report.py
  docs/
    backups/               # safety copies of edited files
    reports/               # generated evidence reports
```

---

## 4. Data model

| Table                    | Key                                             | Purpose                                                                               |
| ------------------------ | ----------------------------------------------- | ------------------------------------------------------------------------------------- |
| `coldchain-shipments`    | `shipment_id`                                   | Which device rides with which shipment and product. `monitored` flag for the watchdog |
| `coldchain-products`     | `product_id`                                    | Temperature and humidity limits, `sustain_count`, `recover_count`                     |
| `coldchain-readings`     | `shipment_id`, `ts_id` (`timestamp#message_id`) | Every accepted reading, with TTL                                                      |
| `coldchain-sensor-state` | `device_id`                                     | Last seen, streak counters, locks `active_excursion_id` and `active_gap_id`           |
| `coldchain-excursions`   | `excursion_id`                                  | Excursion and gap records. Indexes `by_shipment`, `by_status`                         |

Demo data: `SHP-1001` with `coldchain-sensor-01` (product `VAC-2-8C`, monitored) and `SHP-1002` with
`coldchain-sensor-02` (product `INS-2-8C`, not monitored).

### Excursion record

Two separate statuses are kept on purpose:

- `condition_state`: `ACTIVE` or `RECOVERED`. What the temperature is doing.
- `workflow_status`: `OPEN`, `ACKNOWLEDGED`, or `CLOSED`. What humans did.

`RELEASE` is only allowed when the condition is `RECOVERED`. A `type` field is `TEMPERATURE` or
`TELEMETRY_MISSING` (records created before the field existed are treated as `TEMPERATURE`).

### Evidence in S3

```
raw/<device>/<date>/...json            every original message, untouched
rejected/...json                        messages that failed validation, with reasons
evidence/<EXC-id>/opened.json           record and triggering readings
evidence/<EXC-id>/closed.json           recovery statistics
evidence/<EXC-id>/disposition.json      the human decision
```

---

## 5. API

Authentication: Cognito user pool, `USER_PASSWORD_AUTH`, JWT sent to an API Gateway JWT authorizer.
Authorization: the Cognito group in the token.

| Route                                     | Quality | Operations | Auditor |
| ----------------------------------------- | ------- | ---------- | ------- |
| `GET /excursions`, `GET /excursions/{id}` | yes     | yes        | yes     |
| `GET /shipments`, `GET /shipments/{id}`   | yes     | yes        | yes     |
| `POST /excursions/{id}/ack`               | yes     | yes        | no      |
| `POST /excursions/{id}/disposition`       | yes     | no         | no      |
| `GET /excursions/{id}/evidence`           | yes     | no         | yes     |

Error mapping examples: `400` bad input, `401` no or bad token, `403` wrong role, `404` not found,
`409` state conflict (`not_open`, `not_acknowledged`, `release_requires_recovery`,
`release_requires_justification`).

---

## 6. Key design decisions

- **Plain values in transactions.** `boto3` converts Python values to DynamoDB format once. Converting by hand
  first causes double conversion and a `ValidationException`.
- **Claim the slot.** An excursion or gap is opened by one transaction that claims `active_excursion_id` or
  `active_gap_id` and creates the record together, so parallel runs cannot create duplicates.
- **Race guard.** The watchdog's transaction requires `last_seen` to still equal the value it read, so a reading
  that arrives mid-decision cancels the gap.
- **`last_seen` is AWS time.** A device with a wrong or faked clock cannot pretend to be present.
- **A gap is an excursion with a type.** One table, one workflow, one API, one evidence layout.
- **Release after a gap needs a written justification** (at least 20 characters), because the temperature was
  unknown.
- **Producer and consumer agree on fields.** The workflow email reads `$.detail.type`, so every producer sends it.
- **Idempotent notification.** The workflow claims `notified_at` before sending, so a replay cannot send a second
  email.
- **Least privilege.** Each Lambda has its own role. The watchdog can only `PutItem` on excursions.
- **Separate alarm topic.** Platform health (`coldchain-ops-alarms`) is kept apart from medicine incidents
  (`coldchain-alerts`).

---

## 7. Getting started

### Prerequisites

- An AWS account and an IAM user with admin rights (not the root user)
- AWS CLI configured with region `ap-south-1`
- Terraform 1.10 or newer
- Python 3.11 or newer with a virtual environment
- An email address you can confirm SNS subscriptions from

### Configure

Create `terraform/terraform.tfvars` (it is git-ignored):

```hcl
alert_email = "you@example.com"
```

Optional variables (defaults in `terraform/variables.tf`):

| Variable                     | Default | Meaning                                                   |
| ---------------------------- | ------- | --------------------------------------------------------- |
| `poll_seconds`               | 15      | Workflow polling interval                                 |
| `ack_escalate_seconds`       | 90      | Time before an escalation email                           |
| `ack_expire_seconds`         | 240     | Time before an unacknowledged excursion ends the workflow |
| `disposition_expire_seconds` | 300     | Time allowed to record a disposition                      |
| `watchdog_enabled`           | false   | Turns the 1-minute silent-sensor schedule on              |
| `max_silence_seconds`        | 120     | Silence that opens a telemetry gap                        |

The Terraform state uses an S3 backend (`coldchain-tfstate-<account-id>`) with a lock file. Create that bucket
before the first `terraform init`.

### Deploy

```powershell
cd terraform
terraform init
terraform fmt
terraform validate
terraform plan -out step.tfplan     # read the "Plan: X to add, Y to change, Z to destroy" line
terraform apply "step.tfplan"
Remove-Item step.tfplan             # plan files can hold sensitive values
```

After the first apply, **confirm the SNS email subscriptions** from your inbox. Until you click the link, a
subscription stays `PendingConfirmation` and no mail is delivered.

Terraform writes the device certificates and keys to `simulator/certs/` (git-ignored). Never commit them.

### Demo users

Create or reset the Cognito users (`quality1`, `ops1`, `auditor1`, `nogroup1`) with a password of your choice, using
`aws cognito-idp admin-set-user-password ... --permanent`. Never commit or share the password.

### Load variables in a new terminal

```powershell
$EP  = terraform -chdir=terraform output -raw iot_endpoint
$URL = terraform -chdir=terraform output -raw api_url
$SM  = terraform -chdir=terraform output -raw state_machine_arn
```

Shell variables do not survive a new terminal, so print them before use.

---

## 8. Running and testing

### Send readings

```powershell
python simulator\simulator.py --endpoint $EP --sensor sensor-01 --mode hot --count 3      # opens an excursion
python simulator\simulator.py --endpoint $EP --sensor sensor-01 --mode normal --count 3   # recovers it
```

Modes: `normal`, `hot`, `cold`, `humid`.

### Walk through an incident

```powershell
python tests\api.py call --url $URL --user ops1 POST /excursions/latest/ack --note "checking"
python tests\api.py call --url $URL --user ops1 POST /excursions/latest/disposition --decision QUARANTINE --note "x"      # 403
python tests\api.py call --url $URL --user quality1 POST /excursions/latest/disposition --decision QUARANTINE --note "x"  # 200
```

Acknowledge within the escalation window (default 90 seconds) to avoid the escalation email.

### Automated tests

| Script                                 | Result | Covers                                                                     |
| -------------------------------------- | ------ | -------------------------------------------------------------------------- |
| `python tests\send_cases.py`           | 17/17  | Validation, dedupe, late data, identity checks                             |
| `python tests\detect_cases.py`         | 18/18  | Thresholds, glitch filter, relapse, recovery, evidence, per-product limits |
| `python tests\api.py cases --url $URL` | 19/19  | Authentication, roles, validation, evidence access                         |
| `python tests\gap_cases.py`            | 16/16  | Watchdog detection, no duplicates, recovery, justification rule            |

Notes:

- `detect_cases.py` opens excursions that nobody acknowledges. Their workflows end with `AckExpired` after the
  expiry time, which fires the `coldchain-workflow-failed` alarm. This is expected and clears by itself.
- `gap_cases.py` refuses to run while sensor-01 still has an active excursion or gap.
- The watchdog accepts `max_silence_seconds` (minimum 5) in its invoke payload so tests do not wait minutes. It is a
  test hook and should be removed in production.

### Evidence report

```powershell
python tests\report.py latest                    # or an excursion id such as EXC-20261106-13018c17
```

Prints a timeline, measurements, and the SHA-256 of each evidence file, and saves a copy in `docs/reports/`.

### Silent-sensor schedule

The schedule is created **disabled**. To use it, set `watchdog_enabled = true` in `terraform.tfvars` and apply.
Turn it off again when the simulator is idle, or it will alert every time. Emergency brake without Terraform:

```powershell
aws events disable-rule --name coldchain-watchdog-schedule
```

---

## 9. Known limits

- A device that has never reported is not detected, because there is no `last_seen` to measure from.
- Detection delay for silence is the threshold plus up to 60 seconds of schedule granularity.
- There is no heartbeat alarm on the watchdog itself ("who watches the watchman").
- An expired workflow leaves the excursion record `OPEN`. It fails loudly with an alarm but never auto-closes,
  because closing is a human decision.
- The workflow-failed alarm cannot tell a deliberate `AckExpired` from a real error.
- Detection is count-based, not time-based.
- Writing to the database and emitting the event are two steps. A failed event emit is logged only.
- Group changes apply when the user's token expires. No per-site scoping. `USER_PASSWORD_AUTH` with MFA off.
- IoT logging and a dead-letter queue for failed ingest messages are not implemented.
- **KMS:** encryption at rest uses AWS-managed keys (S3 SSE-S3, plus the default encryption of DynamoDB, SNS, and
  log groups). A customer-managed KMS key is the planned upgrade, for key-level audit and revocation.

---

## 10. Security notes

- Never commit `*.pem`, `*.key`, `terraform.tfvars`, `.terraform/`, state files, plan files, `.venv`, or
  `simulator/certs/`. The `.gitignore` covers them.
- Do not put passwords in files or chat. `tests/api.py` reads the password from a hidden prompt or the
  `COLDCHAIN_PW` environment variable.
- The S3 buckets block public access, enforce TLS, and use server-side encryption. The evidence bucket has
  versioning on.
- Lambda zips are built from the whole folder, so keep stray files (such as `__pycache__`) out of `lambdas/*`.

---

## 11. Cost and clean-up

The project is sized for a short demo. A budget alert is set at $10 per month. Costs come mainly from CloudWatch
logs, Lambda invocations while the watchdog schedule is on, and the CloudTrail and log buckets.

When you are finished, after the demo and review:

```powershell
cd terraform
terraform destroy
```

The raw bucket and the CloudTrail bucket use `force_destroy = true`, so they delete even when not empty. Destroying
removes the live system, so take screenshots and run the demo first.

---

## 12. Project status

| Step    | Content                                                                                       | Status                               |
| ------- | --------------------------------------------------------------------------------------------- | ------------------------------------ |
| 0 to 11 | Foundation: state, budget, S3, IoT, ingest, DynamoDB, detection, events, workflow, API, roles | Done                                 |
| 12      | Missing telemetry watchdog                                                                    | Done                                 |
| 13      | CloudWatch alarms, CloudTrail, API access logs                                                | Done                                 |
| 14      | KMS customer-managed keys                                                                     | Skipped (planned upgrade)            |
| 15      | Evidence report                                                                               | Done                                 |
| 16      | Final tests, demo, `terraform destroy`                                                        | Tests done, destroy after the review |
