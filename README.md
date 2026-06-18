# Users Lambda — CRUD Controller for DynamoDB

AWS Lambda function providing a RESTful CRUD API for user profiles, backed by a DynamoDB single-table design.

## Architecture

```
API Gateway (REST or HTTP API v2)
        │
        ▼
  lambda_handler()
        │
        ├── POST   → Create User
        ├── GET    → Read User
        ├── PUT    → Update User
        ├── PATCH  → Update User
        ├── DELETE → Delete User
        └── OPTIONS → CORS Preflight
```

The function routes requests based on the HTTP method extracted from the API Gateway proxy event. Both REST API (`event['httpMethod']`) and HTTP API v2 (`event['requestContext']['http']['method']`) formats are supported.

The `boto3` DynamoDB resource and table reference are instantiated at module level outside the handler for connection reuse across warm Lambda invocations.

## DynamoDB Schema

The table uses a **Single-Table Design** with generic key attributes:

| Attribute | Type   | Pattern              |
|-----------|--------|----------------------|
| `PK`      | String | `USER#<userId>`      |
| `SK`      | String | `METADATA` (literal) |

### User Profile Fields

| Field              | Type    | Description                        |
|--------------------|---------|------------------------------------|
| `userId`           | String  | Unique user identifier             |
| `username`         | String  | Display name                       |
| `password`         | String  | Pre-hashed password (never returned in responses) |
| `firstName`        | String  | First name                         |
| `lastName`         | String  | Last name                          |
| `email`            | String  | Email address                      |
| `language`         | String  | Preferred language code             |
| `timeZone`         | String  | IANA timezone                      |
| `firstLogin`       | String  | ISO 8601 timestamp                 |
| `lastLogin`        | String  | ISO 8601 timestamp                 |
| `mfaEnabled`       | Boolean | Whether MFA is enabled             |
| `newsletter`       | Boolean | Newsletter subscription status     |
| `createdTimestamp`  | String  | Auto-set on creation (UTC ISO 8601)|
| `updatedTimestamp`  | String  | Auto-set on create/update (UTC ISO 8601) |
| `checklists`       | Map     | User checklists                    |
| `tripLog`          | Map     | Trip log entries                   |
| `passportStamps`   | List    | Passport stamp records             |

## API Reference

### Create User — `POST`

**Request body:**
```json
{
  "userId": "u-001",
  "username": "jdoe",
  "password": "pre_hashed_value",
  "firstName": "John",
  "lastName": "Doe",
  "email": "jdoe@example.com",
  "language": "en",
  "timeZone": "America/New_York",
  "mfaEnabled": true,
  "newsletter": false,
  "checklists": {},
  "tripLog": {},
  "passportStamps": []
}
```

**Responses:**
- `201` — User created. Returns the user object (without `password`).
- `400` — Missing `userId` or user already exists.

Uses `attribute_not_exists(PK)` condition to prevent overwrites.

### Read User — `GET`

**Query string:** `?userId=u-001`

**Responses:**
- `200` — Returns the user object (without `password`).
- `400` — Missing `userId` query parameter.
- `404` — User not found.

### Update User — `PUT` / `PATCH`

**Request body:**
```json
{
  "userId": "u-001",
  "email": "new@example.com",
  "checklists": {"packing": {"items": ["passport"]}}
}
```

Only include fields you want to update. `updatedTimestamp` is set automatically. The `PK`, `SK`, and `userId` fields cannot be modified. Unknown fields outside the schema are silently ignored.

Nested types (`checklists`, `tripLog`, `passportStamps`) are replaced at the top level — the entire attribute value is overwritten, not deep-merged.

**Responses:**
- `200` — User updated. Returns the full updated user object (without `password`).
- `400` — Missing `userId`.
- `404` — User does not exist (`attribute_exists(PK)` condition failed).

#### How the Dynamic UpdateExpression Works

The handler builds the DynamoDB `UpdateExpression` at runtime from whichever fields the caller provides:

1. Filter the request body to only known schema fields, excluding reserved keys (`PK`, `SK`, `userId`).
2. Inject `updatedTimestamp` with the current UTC time.
3. For each field, generate indexed placeholders — `#f0`/`:v0`, `#f1`/`:v1`, etc. — avoiding DynamoDB reserved word conflicts.
4. Assemble into a single `SET` clause: `SET #f0 = :v0, #f1 = :v1, ...`.
5. Pass `ExpressionAttributeNames` and `ExpressionAttributeValues` maps alongside the expression.

### Delete User — `DELETE`

**Query string:** `?userId=u-001`
Or **request body:** `{"userId": "u-001"}`

Query string takes precedence if both are provided.

**Responses:**
- `200` — User deleted.
- `400` — Missing `userId`.

## Environment Variables

| Variable     | Default                  | Description          |
|-------------|--------------------------|----------------------|
| `TABLE_NAME` | `unstamped-pages-prod`   | DynamoDB table name  |

## Security

- The `password` field is **never** included in any API response.
- All responses include CORS headers for browser compatibility.
- Condition expressions prevent accidental overwrites (create) and phantom updates (update).
- Only whitelisted schema fields are accepted — unknown fields are dropped.

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) (package manager / lockfile tooling)
- AWS credentials configured (via environment, IAM role, or AWS profile)
- A DynamoDB table with `PK` (String) and `SK` (String) key schema

## Setup

```bash
# Install all dependencies (production + dev) from the lockfile
uv sync --extra dev
```

This creates a `.venv` and installs exactly what `uv.lock` specifies — no resolution, fully deterministic.

## Running Tests

```bash
# Run the full suite (coverage flags are configured in pyproject.toml)
.venv/bin/pytest
```

pytest is configured via `pyproject.toml` to automatically:
- Run with `-v` (verbose)
- Generate a terminal coverage report with missing lines
- Write `coverage.xml` for SonarCloud ingestion
- Enforce a 95% minimum coverage gate

All tests use mocked DynamoDB calls — no AWS credentials or network access required.

## CI / SonarCloud

The GitHub Actions workflow (`.github/workflows/users-lambda.yml`) runs on every push and PR to `master`:

1. Checks out with full history (required for Sonar blame)
2. Sets up uv + Python 3.14
3. Installs dependencies with `uv sync --extra dev --frozen --no-build`
4. Runs pytest (generates `coverage.xml`)
5. Runs SonarCloud analysis (config in `sonar-project.properties`)

**Required repository secrets:**
- `SONAR_TOKEN` — generated in SonarCloud under the `jpdust_users-lambda` project

## Deployment

### Zip Package

```bash
# Install production dependencies into a package directory
uv pip install boto3 --target package/
cp lambda_function.py package/

# Create the deployment zip
cd package && zip -r ../deployment.zip . && cd ..
```

Upload `deployment.zip` to Lambda with:
- **Runtime:** Python 3.14
- **Handler:** `lambda_function.lambda_handler`
- **Environment variable:** `TABLE_NAME=unstamped-pages-prod`
- **IAM permissions:** `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem` on the target table

### SAM / CloudFormation

A `template.yaml` is included in the repo. Deploy with:

```bash
sam build
sam deploy --guided
```

This provisions the DynamoDB table, Lambda function (with IAM policy), and API Gateway routes.

## Project Structure

```
users-lambda/
├── .github/
│   └── workflows/
│       └── users-lambda.yml        # CI: test + SonarCloud analysis
├── tests/
│   ├── __init__.py
│   └── test_lambda_function.py     # Full unit test suite (59 tests)
├── .gitignore
├── lambda_function.py              # Lambda handler and CRUD logic
├── pyproject.toml                  # Project metadata, pytest & coverage config
├── requirements.txt                # Production dependencies
├── requirements-dev.txt            # Dev dependencies
├── sonar-project.properties        # SonarCloud configuration
├── template.yaml                   # SAM template (Lambda + DynamoDB + API GW)
└── uv.lock                         # Locked dependency graph
```
