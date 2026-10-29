# Authentication Setup Guide

This guide covers how to configure authentication for Context Ontology Accelerator (COA). The platform supports AWS Cognito User Pools, any OIDC-compliant identity provider, and SAML 2.0 or OIDC providers federated through Cognito.

## Overview

COA uses **OIDC Authorization Code flow with PKCE** for all user authentication. Every API request requires a valid Bearer token issued by your configured identity provider.

```mermaid
sequenceDiagram
    participant User
    participant WebApp
    participant IdP as Identity Provider
    participant API as COA API Gateway

    User->>WebApp: Navigate to app
    WebApp->>IdP: Redirect (PKCE challenge)
    IdP->>User: Login prompt
    User->>IdP: Credentials
    IdP->>WebApp: Authorization code
    WebApp->>IdP: Exchange code for tokens
    IdP->>WebApp: ID token + access token
    WebApp->>API: API call (Bearer token)
    API->>API: Lambda Authorizer validates token
    API->>WebApp: Response
```

## Option A: AWS Cognito User Pool

If you deployed with `make deploy-dev` using the default `idpType: COGNITO`
(no `oidcSettings` configured — see "Option B: External OIDC Provider" below
for using your own IdP instead), the User Pool, app client, and an initial
admin user are **already provisioned automatically** — there is nothing left
to create manually for a first login.

### What's Already Set Up

The `coa-dev-auth` stack (`IdpAuthenticationStack`) creates, at deploy time:

- A Cognito **User Pool** with a hosted UI domain
- A **web app client** (Authorization Code + PKCE, no client secret) used by the React app
- A separate **MCP/CLI client** (`localhost:9876` redirect) for IDE integrations
- An **`Admin` group**, pre-seeded in the authorization tables so that any user
  in this group is automatically granted the `platform-admin` role — no
  manual grant needed
- An **initial admin user**, created via a CDK custom resource, added to the
  `Admin` group, with a Cognito-generated temporary password emailed to them
  (the user must change it on first login)

Find these values in the CloudFormation outputs:

```bash
aws cloudformation describe-stacks \
  --stack-name coa-dev-auth \
  --query "Stacks[0].Outputs" \
  --output table
```

Key outputs:
- `UserPoolId` — the Cognito User Pool ID
- `UserPoolClientId` — the web app client ID
- `McpClientId` — the MCP/CLI client ID
- `CognitoDomainUrl` — the hosted UI domain

**To sign in as the initial admin:** the default username/email is a
placeholder (`nobody@amazon.com`) that you cannot receive mail at — the
temporary password Cognito emails on user creation would go nowhere useful.
**Set `initialAdminEmail` in the SSM config at `/<prefix>/config` before your
first deploy** so the admin user is created under an email you control:

```json
{
  "initialAdminEmail": "you@yourcompany.com"
}
```

If you already deployed with the placeholder, redeploying with
`initialAdminEmail` set creates a **new, separate** admin user under your
real email (the username is derived from the email, so it doesn't rename or
update the placeholder user — the old `nobody@amazon.com` user is left
behind, unused, in the User Pool). Alternatively, create a user manually and
add it to the `Admin` group (see "Add Additional Users" below).

### Adding Additional Users and Groups

Everything below this point is **optional** — only needed if you want
additional users beyond the auto-provisioned admin, or additional
role-mapped groups beyond `Admin`.

#### Configure Additional Groups for Role Mapping

The `Admin` group → `platform-admin` mapping is pre-seeded automatically.
For any other role, create the Cognito group and grant the corresponding
role via the [Role & Permission Management Guide](role-permission-management.md)
— creating a group in Cognito does **not** by itself grant any role; only
`Admin` is pre-wired:

```bash
# Create an additional group (still needs an explicit role grant — see above)
aws cognito-idp create-group \
  --user-pool-id us-east-1_XXXXX \
  --group-name data-stewards \
  --description "Data stewards across namespaces"
```

Group memberships are included in the `cognito:groups` claim of the ID token. The COA authorizer reads this claim to resolve group-based grants.

#### Add Additional Users

```bash
aws cognito-idp admin-create-user \
  --user-pool-id us-east-1_XXXXX \
  --username alice@company.com \
  --user-attributes Name=email,Value=alice@company.com

aws cognito-idp admin-add-user-to-group \
  --user-pool-id us-east-1_XXXXX \
  --username alice@company.com \
  --group-name Admin
```

#### Email changes need verification

Grants are looked up by the email in the user's ID token, so the user pool
only applies a user's own email change after they verify the new address:
Cognito emails a code to the new address and keeps the old email, in the pool
and in tokens, until the user confirms it. Users cannot mark an email as
verified themselves. An administrator can still change a user's email directly
with `admin-update-user-attributes`; include `Name=email_verified,Value=true`
to apply it without a code. Review the user's grants when you do, because
grants made for the new address apply to them from their next sign-in.

When you upgrade a deployment from a release without this setting, the setting
does not undo email changes made before the upgrade: until then a user's own
change applied at once and left `email_verified` set to `false`. After the
upgrade, list the users whose email is not verified and check that each email
belongs to that person:

```bash
aws cognito-idp list-users --user-pool-id us-east-1_XXXXX \
  --query "Users[?Attributes[?Name=='email_verified' && Value=='false']].[Username, Attributes[?Name=='email'].Value | [0], UserLastModifiedDate]" \
  --output table
```

Users created with `admin-create-user` without `Name=email_verified,Value=true`
also appear in this list. For any user whose email is not theirs, set it back
with `email_verified=true` (without it, the change waits for verification and
the user keeps the wrong email), sign them out so their refresh token stops
issuing tokens, and remove grants they should not have:

```bash
aws cognito-idp admin-update-user-attributes --user-pool-id us-east-1_XXXXX \
  --username <username> \
  --user-attributes Name=email,Value=<their-own-email> Name=email_verified,Value=true
aws cognito-idp admin-user-global-sign-out --user-pool-id us-east-1_XXXXX \
  --username <username>
```

ID tokens already issued stay valid until they expire.

## Option B: External OIDC Provider

COA supports any OIDC-compliant provider (Okta, Azure AD, Auth0, Keycloak, etc.).

### 1. Register COA as a Client

In your identity provider:

1. Create a new OIDC application (SPA / public client)
2. Set the **redirect URI** to `https://<your-app-domain>/authenticate/`
3. Set the **post-logout redirect** to `https://<your-app-domain>/`
4. Enable **Authorization Code flow with PKCE**
5. Request scopes: `openid`, `email`, `profile`

### 2. Configure Group Claims

The authorizer reads group memberships from the ID token. Configure your IdP to include groups in a claim, then set the matching claim name as `groupClaim` in the `oidcSettings` SSM config (see "3. Configure the Authorizer Lambda" below) — this becomes the `GROUP_CLAIM_NAME` environment variable on the authorizer Lambda automatically (default: `"groups"`).

| Provider | Claim name | Configuration |
|----------|-----------|---------------|
| Okta | `groups` | Add "groups" scope, configure group claim in authorization server |
| Azure AD | `groups` | Configure token claims in App Registration → Token Configuration |
| Keycloak | `groups` | Add "groups" mapper to client scope |
| Auth0 | `https://your-app/groups` | Add a Rule/Action to include roles in token; set `groupClaim` in `oidcSettings` to match |

### 3. Configure the Authorizer Lambda

You do **not** set these directly as Lambda environment variables — they're
derived automatically from the same `oidcSettings` you write to SSM (see
"Step 5: Deploy Context Ontology Accelerator" in the Okta walkthrough below) and wired onto the authorizer's environment at deploy time via
`ApiStack`'s CDK props (`issuerUrl`, `jwksUri`, `clientId`, `groupClaimName`
in `bin/app.ts`). Setting the right value in `oidcSettings` is all that's
needed — there is no separate authorizer configuration step for a standard
CDK deploy.

| SSM `oidcSettings` field | Resulting Lambda env var | Value |
|---|---|---|
| `issuerUrl` | `JWKS_ISSUER` | Your OIDC issuer URL (e.g. `https://login.microsoftonline.com/{tenant}/v2.0`) |
| `jwksUri` | `JWKS_URI` | (Optional) Explicit JWKS endpoint if not at `{issuer}/.well-known/jwks.json` |
| `clientId` | `CLIENT_ID` | Your app's client ID (used as expected `aud` claim) |
| `groupClaim` | `GROUP_CLAIM_NAME` | Token claim containing group list (default: `"groups"`) |

If you're deploying outside CDK entirely (a non-standard setup), you'd set
these Lambda environment variables directly on the authorizer function —
but for `make deploy-dev`, editing the SSM config is the only step required.

## Option C: Federate SAML or OIDC Providers Through Cognito

Use this option when users should sign in with your corporate identity
provider but you want to keep the Cognito User Pool from Option A. Compared
with Option B:

- It works with **SAML 2.0** providers as well as OIDC. Option B is OIDC only.
- You can add **several providers**, and Cognito users, including the initial
  admin, can still sign in with a password.
- COA still receives **Cognito ID tokens**, so the authorizer, AgentCore
  runtimes and web app config work exactly as in Option A.

Cognito's hosted login page shows a button for each provider next to the
username and password form. Both the web app client and the MCP/CLI client
offer every provider.

### 1. Deploy and Note the User Pool Values

Your IdP needs two values from the user pool, and the user pool ID only exists
after the first deploy. Deploy with the default Cognito config first, or use an
existing deployment, then read `UserPoolId` and `CognitoDomainUrl` from the
`coa-dev-auth` stack outputs (see "What's Already Set Up" above).

Adding providers to an existing deployment works: the next deploy adds them to
both app clients.

### 2. Register the User Pool with Your IdP

For a **SAML** provider, create a SAML application in your IdP:

| Setting | Value |
|---------|-------|
| ACS URL (single sign-on URL) | `<CognitoDomainUrl>/saml2/idpresponse` |
| SP entity ID (audience URI) | `urn:amazon:cognito:sp:<UserPoolId>` |
| Attributes | Include the user's email address in the assertion |

Then copy the IdP's metadata URL, or download its metadata XML.

For an **OIDC** provider, create a web application (a confidential client,
with a client secret) in your IdP:

| Setting | Value |
|---------|-------|
| Redirect URI | `<CognitoDomainUrl>/oauth2/idpresponse` |
| Scopes | `openid`, `email`, `profile` |
| Client authentication | `client_secret_post` (the only method Cognito uses) |

Note the app's client ID, client secret and issuer URL.

### 3. Add the Providers to the Deploy Config

Add these keys to your existing `/<prefix>/config` JSON, then run
`make deploy-dev`. A SAML example:

```json
{
  "idpType": "SAML",
  "samlProviders": [
    {
      "name": "CorpSSO",
      "metadataUrl": "https://idp.example.com/app/abc123/sso/saml/metadata"
    }
  ],
  "attributeMapping": {
    "email": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
  }
}
```

An OIDC example:

```json
{
  "idpType": "COGNITO",
  "oidcProviders": [
    {
      "name": "PartnerIdP",
      "issuerUrl": "https://login.partner.example.com",
      "clientId": "0oa...",
      "clientSecret": "...",
      "scopes": ["openid", "email", "profile"]
    }
  ],
  "attributeMapping": {
    "email": "email"
  }
}
```

| Key | What to set |
|-----|-------------|
| `idpType` | `SAML` or `COGNITO`. Both create the User Pool and register the providers below. With `OIDC` (Option B) the provider lists are ignored. |
| `samlProviders[]` | `name`, plus one of `metadataUrl` or `metadataContent` (the metadata XML as a JSON string). |
| `oidcProviders[]` | `name`, `issuerUrl`, `clientId`, `clientSecret`, and optional `scopes` (default `openid`, `email`, `profile`). |
| `attributeMapping` | Cognito attribute → the IdP's attribute name. One mapping applies to every provider in both lists. |

`name` is the label on the login button and the provider's name in Cognito. It
must be 3 to 32 characters, with no spaces or underscores.

**Map `email`.** The authorizer uses the ID token's `email` claim as the user's
principal and rejects tokens without it. Map it from the attribute your IdP
sends: usually `email` for OIDC, and for SAML the attribute name in the
assertion, such as
`http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress` from
Microsoft Entra ID or AD FS. Because the mapping is shared, providers in one
deployment have to send email under the same attribute name.

The OIDC `clientSecret` is written into the `coa-dev-auth` CloudFormation
template.

### 4. Grant Roles to Federated Users

COA reads group memberships from the `cognito:groups` claim, and groups from
your IdP don't appear there. Cognito instead adds every user who signs in
through a provider to a group named `<UserPoolId>_<name>`, for example
`ap-northeast-1_EXAMPLE_CorpSSO`.

To give everyone from a provider a platform role, add a `claimsMappings` entry
for that group in the same deploy as step 3:

```json
{
  "claimsMappings": [
    {
      "groupValue": "ap-northeast-1_EXAMPLE_CorpSSO",
      "mappedRoles": ["platform-viewer"]
    }
  ]
}
```

For namespace roles or individual users, grant roles to that group or to the
user's email as described in the
[Role & Permission Management Guide](role-permission-management.md).
Federated users aren't in the `Admin` group, so sign in as the initial admin to
make the first grants.

## Configure the Web App

When you deploy with CDK, the web app's runtime configuration
(`runtime-config.json`) is **generated and injected into S3 automatically** —
no manual editing required. The `WebStack` builds it from the deployed stack
outputs (OIDC authority, client ID, API endpoint, and region) so the React app
discovers its backends without a rebuild:

```json
{
  "region": "us-east-1",
  "stage": "dev",
  "authority": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXXX",
  "clientId": "your-client-id",
  "apiEndpoint": "https://your-api.execute-api.us-east-1.amazonaws.com/prod/",
  "serveRuntimeArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/my-runtime"
}
```

| Field | Source (set automatically on deploy) |
|-------|--------------------------------------|
| `region` | Deployment region |
| `stage` | Environment name (e.g. `dev`) |
| `authority` | OIDC issuer URL. For Cognito: `https://cognito-idp.{region}.amazonaws.com/{user-pool-id}` |
| `clientId` | The OAuth client ID from the auth stack |
| `apiEndpoint` | The deployed API Gateway endpoint |
| `serveRuntimeArn` | AgentCore Runtime ARN for SSE streaming queries |

!!! note
    You only need to set these values manually when running the web app outside
    of a CDK deployment (for example, local development against a remote
    backend). For standard deployments, `cdk deploy` handles it.

## How Role Resolution Works

When a user authenticates, the authorizer resolves their permissions:

1. **Validate token** — Verify JWT signature against the JWKS endpoint
2. **Extract principal** — Read `email` (or `sub`) as the principal ID
3. **Extract groups** — Read the configured group claim
4. **Query grants** — Look up grants for `User::{email}` and `Group::{group}` in DynamoDB
5. **Evaluate Cedar** — Load policies for the resolved roles and evaluate the request

Grants can be assigned to individual users OR to IdP groups. When a user belongs to a group that has a grant, they inherit that role.

## Verifying Authentication

After setup, verify the flow works end-to-end:

```bash
# Get a token (via Cognito hosted UI or your IdP's token endpoint)
TOKEN="eyJ..."

# Test an API call
curl -s -H "Authorization: Bearer $TOKEN" \
  https://your-api.execute-api.us-east-1.amazonaws.com/prod/namespaces \
  | python3 -m json.tool
```

A successful response returns your namespace list. A `403` indicates the token is valid but the user lacks permissions — see the [Role & Permission Management Guide](role-permission-management.md) to grant access.

## Token Usage

Context Ontology Accelerator uses **ID tokens** for all authentication across every interface:

| Interface | Token type | Key claims |
|-----------|-----------|------------|
| **REST API** (API Gateway) | ID token | `aud`, `email`, `groups` |
| **Playground** (AgentCore SSE) | ID token | `aud`, `email`, `groups` |
| **MCP** (AgentCore Runtime) | ID token | `aud`, `email`, `groups` |

The ID token carries the user's identity (`email`), group memberships (`groups` claim), and audience (`aud` = client ID). AgentCore Runtime validates the `aud` claim — which is present on ID tokens from any OIDC-compliant provider. Access tokens are **not used**.

### Getting a token (dev only)

```bash
# ID token (used for all APIs)
aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH \
  --client-id <CLIENT_ID> --auth-parameters USERNAME=<email>,PASSWORD=<pass> \
  --region <region> --query "AuthenticationResult.IdToken" --output text
```

Production uses Authorization Code + PKCE (handled by the web app automatically).

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Login redirect fails | Callback URL mismatch | Verify redirect URI in IdP matches `<origin>/authenticate/` exactly |
| `401 Unauthorized` | Token expired or malformed | Check token expiry; verify the `authority` URL is correct |
| `403 Forbidden` after login | User has no grants | Assign a role via the Identity page or API (see [Role & Permission Management](role-permission-management.md)) |
| Groups not appearing | Wrong claim name | Verify `GROUP_CLAIM_NAME` matches your IdP's claim; inspect the raw ID token |
| "Token refresh failed" in browser | Silent refresh blocked | Ensure your IdP allows iframe-based token refresh, or reduce token lifetime |
| `Token is missing the "aud" claim` | ID token misconfigured | Verify the authorization server audience matches your app's client ID |
| `Token missing required email claim` | Email claim not included | Ensure the IdP includes `email` in the ID token claims |

---

## Okta Setup (Step-by-Step)

This walkthrough covers configuring Okta as the external OIDC identity provider for Context Ontology Accelerator (Option B above).

### Prerequisites

- An Okta tenant (e.g. `https://your-org.okta.com`)
- Admin access to create applications and authorization servers in Okta
- (Optional) Your Context Ontology Accelerator UI custom domain. If not using custom domains, you can update callback URLs after deployment using the generated CloudFront domain.

### Step 1: Create an Application in Okta

1. Navigate to **Applications → Create App Integration**
2. Select **OIDC - OpenID Connect** as sign-in method
3. Select **Single-Page Application** as application type, then press **Next**
4. Configure the application:

| Setting | Value |
|---------|-------|
| App integration name | `Context Ontology Accelerator` (or name of your choice) |
| Grant type | **Authorization Code** + **Refresh Token** |
| Sign-in redirect URIs | `https://<UI_DOMAIN>/authenticate/` and `http://localhost:9876/oauth/callback` (for MCP/CLI) |
| Sign-out redirect URIs | `https://<UI_DOMAIN>/` |
| Assignments | Select users/groups who should access Context Ontology Accelerator |

5. Click **Save**
6. On the application page, note the **Client ID**

!!! tip
    If you don't have a custom domain yet, use placeholder URLs. After deployment, update with the CloudFront domain from the stack outputs.

!!! note "No client secret needed"
    Since the application type is **Single-Page Application**, Okta issues a
    public client with no client secret (per OAuth 2.0 PKCE for public clients,
    RFC 8252) — you'll only need the **Client ID**. The direct-OIDC integration
    (`idpType: "OIDC"`) has no `clientSecret` field in its config; only the
    separate Cognito-federation path (`oidcProviders`, used when federating an
    external OIDC IdP *through* Cognito rather than replacing it; see "Option C"
    above) accepts one.
    If Okta's app page shows a client secret, you selected the wrong
    application type — switch to Single-Page Application.

### Step 2: Create a Custom Authorization Server

Okta's `default` authorization server cannot include group claims in ID tokens. You **must** create a custom one — the group claim cannot be added to `default`, so skipping this step means role mapping will never work.

1. Navigate to **Security → API → Authorization Servers**
2. Click **Add Authorization Server**

| Setting | Value |
|---------|-------|
| Name | `Context Ontology Accelerator` (or name of your choice) |
| Audience | `https://<UI_DOMAIN>` (or placeholder — update post-deploy) |
| Description | Authorization server for Context Ontology Accelerator |

3. Note the **Issuer URI** (e.g. `https://your-org.okta.com/oauth2/aus...`)

### Step 3: Add a Groups Claim

1. On your custom authorization server, select the **Claims** tab
2. Click **Add Claim**

| Setting | Value |
|---------|-------|
| Name | `groups` |
| Include in token type | **ID Token** |
| Value type | **Groups** |
| Filter | Matches regex `.*` (all groups) |

3. Click **Create**

### Step 4: Create an Access Policy

1. On your custom authorization server, select the **Access Policies** tab
2. Click **Add New Access Policy**

| Setting | Value |
|---------|-------|
| Name | `OntologyAcceleratorAccessPolicy` |
| Description | Access policy for Context Ontology Accelerator |
| Assign to | **All clients** (or restrict to your Context Ontology Accelerator client ID) |

3. Click **Create Policy**, then **Add Rule**:
   - Ensure **Authorization Code** is enabled under "Grant type"
   - Configure user/group scope and token lifetimes as needed (defaults are fine)
   - Ensure requested scopes include **openid** at minimum
4. Click **Create Rule**

### Step 5: Deploy Context Ontology Accelerator

Store the following configuration in SSM Parameter Store at `/<prefix>/config` (JSON):

```json
{
  "idpType": "OIDC",
  "oidcSettings": {
    "issuerUrl": "https://your-org.okta.com/oauth2/aus...",
    "clientId": "0oa...",
    "jwksUri": "https://your-org.okta.com/oauth2/aus.../v1/keys",
    "groupClaim": "groups"
  },
  "claimsMappings": [
    {
      "groupValue": "Context Ontology Accelerator Admins",
      "mappedRoles": ["platform-admin"]
    }
  ]
}
```

Then deploy:

```bash
make deploy-dev
```

The `IdpAuthenticationStack` reads this config and wires the issuer URL + client ID into the API Gateway authorizer, AgentCore Runtime (Serve + MCP), and the web app's `runtime-config.json` — all automatically.

### Step 6: Post-Deploy — Update Okta Settings

If you used placeholder URLs, update them now:

1. Get the CloudFront domain from stack outputs:
   ```bash
   aws cloudformation describe-stacks \
     --stack-name coa-dev-web \
     --query "Stacks[0].Outputs[?OutputKey=='DistributionDomainName'].OutputValue" \
     --output text
   ```

2. **Update the Okta Application:**
   - Sign-in redirect URI → `https://<CloudFront_URL>/authenticate/`
   - Sign-out redirect URI → `https://<CloudFront_URL>/`

3. **Update the Authorization Server Audience:**
   - Navigate to **Security → API → Authorization Servers → your server**
   - Set **Audience** to `https://<CloudFront_URL>`

### Step 7: Verify

1. Navigate to your Context Ontology Accelerator UI URL
2. You should be redirected to Okta's login page
3. After authenticating, verify you land on the Context Ontology Accelerator dashboard
4. Check that your group memberships appear correctly by navigating to the Identity page

### Okta-Specific Notes

- **Group claim format:** Okta sends groups as a JSON array by default, which Context Ontology Accelerator handles natively
- **Token lifetimes:** The MCP client benefits from longer token lifetimes (24h+ ID token). Configure this in the access policy rule under "Token Lifetime"
- **MCP/CLI flow:** The `http://localhost:9876/oauth/callback` redirect URI enables PKCE-based auth for IDE integrations (Kiro, Claude Desktop, Cursor). Add this to your Okta app's allowed redirect URIs
- **Single client:** In OIDC mode, Context Ontology Accelerator uses the same Okta client ID for both the web app and MCP/CLI flows. If you need separate token lifetimes or scopes per interface, register a second application in Okta and pass its client ID via `oidcSettings.mcpClientId` (future enhancement)
