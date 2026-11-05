// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { execFileSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

/**
 * A public latest-AMI SSM parameter can replace an EC2 instance with
 * no template change when used for ImageId and
 * CloudFormation re-resolved it. ssm_refs_check.py lists every SSM reference in an
 * assembly and refuses one that feeds a property whose change replaces the resource.
 */
const CHECK = path.resolve(__dirname, "../../scripts/ssm_refs_check.py");
const AMI_PARAMETER =
  "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-arm64";

function assembly(files: Record<string, unknown>): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cdkout-"));
  for (const [name, body] of Object.entries(files)) {
    fs.writeFileSync(path.join(dir, name), JSON.stringify(body));
  }
  return dir;
}

function check(dir: string): { status: number; output: string } {
  try {
    const output = execFileSync("python3", ["-I", CHECK, dir], {
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "" },
      stdio: "pipe",
    });
    return { status: 0, output };
  } catch (err: unknown) {
    const e = err as { status: number; stdout: string; stderr: string };
    return { status: e.status, output: `${e.stdout}${e.stderr}` };
  }
}

const bootstrap = {
  BootstrapVersion: {
    Type: "AWS::SSM::Parameter::Value<String>",
    Default: "/cdk-bootstrap/hnb659fds/version",
  },
};

function instanceStack(
  imageId: unknown,
  extraParameters: Record<string, unknown> = {},
): unknown {
  return {
    Parameters: { ...bootstrap, ...extraParameters },
    Resources: {
      Probe: {
        Type: "AWS::EC2::Instance",
        Properties: { ImageId: imageId, InstanceType: "t4g.small" },
      },
    },
  };
}

const amiParameter = {
  AmiParam: {
    Type: "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
    Default: AMI_PARAMETER,
  },
};

describe("ssm_refs_check.py", () => {
  it.each([
    ["without a default", {}],
    ["with a null default", { Default: null }],
    ["with an empty default", { Default: "" }],
  ])(
    "refuses an SSM parameter %s on a replacing property",
    (_label, defaults) => {
      const { status, output } = check(
        assembly({
          "probe.template.json": instanceStack(
            { Ref: "AmiParam" },
            {
              AmiParam: {
                Type: "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
                ...defaults,
              },
            },
          ),
        }),
      );
      expect(status).toBe(5);
      expect(output).toMatch(/UNLISTED.*Probe.*ImageId/);
    },
  );

  it("refuses a public SSM parameter that feeds an EC2 instance's ImageId", () => {
    const { status, output } = check(
      assembly({
        "probe.template.json": instanceStack({ Ref: "AmiParam" }, amiParameter),
      }),
    );
    expect(status).toBe(5);
    expect(output).toContain(AMI_PARAMETER);
    expect(output).toMatch(/REFUSE.*probe.*Probe.*ImageId/);
  });

  it("refuses a {{resolve:ssm:...}} dynamic reference that feeds a replacing property", () => {
    const { status, output } = check(
      assembly({
        "probe.template.json": instanceStack(
          `{{resolve:ssm:${AMI_PARAMETER}}}`,
        ),
      }),
    );
    expect(status).toBe(5);
    expect(output).toMatch(/REFUSE.*ImageId/);
  });

  it("finds the reference inside Fn::Join and Fn::Sub too", () => {
    const joined = instanceStack(
      { "Fn::Join": ["", [{ Ref: "AmiParam" }]] },
      amiParameter,
    );
    const subbed = instanceStack({ "Fn::Sub": `\${AmiParam}` }, amiParameter);
    expect(check(assembly({ "a.template.json": joined })).status).toBe(5);
    expect(check(assembly({ "b.template.json": subbed })).status).toBe(5);
  });

  it.each([
    [
      "an ARN-form dynamic reference",
      `{{resolve:ssm:arn:aws:ssm:us-east-1::parameter${AMI_PARAMETER}}}`,
      {},
    ],
    [
      "an ARN-form dynamic reference with pseudo parameters",
      {
        "Fn::Sub": `{{resolve:ssm:arn:\${AWS::Partition}:ssm:\${AWS::Region}:\${AWS::AccountId}:parameter${AMI_PARAMETER}}}`,
      },
      {},
    ],
    [
      "a parameter default without the leading slash",
      { Ref: "AmiParam" },
      {
        AmiParam: {
          Type: "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
          Default: AMI_PARAMETER.slice(1),
        },
      },
    ],
    [
      "a dynamic reference with a pseudo parameter in its name",
      {
        "Fn::Sub":
          "{{resolve:ssm:/aws/service/ami-amazon-linux-latest/${AWS::Region}-arm64}}",
      },
      {},
    ],
    [
      "a version-pinned dynamic reference",
      `{{resolve:ssm:${AMI_PARAMETER}:42}}`,
      {},
    ],
  ])(
    "refuses a public AMI parameter written as %s",
    (_label, imageId, extraParameters) => {
      const { status, output } = check(
        assembly({
          "probe.template.json": instanceStack(imageId, extraParameters),
        }),
      );
      expect(status).toBe(5);
      expect(output).toMatch(/REFUSE\s+public probe Probe\.ImageId/);
    },
  );

  it.each([
    [
      "built with Fn::Join around a token",
      {
        "Fn::Join": ["", ["{{resolve:ssm:", { Ref: "AmiName" }, "}}"]],
      },
    ],
    [
      "that starts with a Fn::Sub variable",
      { "Fn::Sub": "{{resolve:ssm:${AmiName}}}" },
    ],
    [
      "an ARN whose name part is a token",
      {
        "Fn::Sub":
          "{{resolve:ssm:arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter${AmiName}}}",
      },
    ],
    [
      "an ARN in another account (a shared parameter can move too)",
      "{{resolve:ssm:arn:aws:ssm:us-east-1:999999999999:parameter/shared/ami-id}}",
    ],
  ])(
    "refuses a dynamic reference whose name is %s (unresolved identity)",
    (_label, imageId) => {
      const { status, output } = check(
        assembly({
          "probe.template.json": instanceStack(imageId, {
            AmiName: { Type: "String", Default: AMI_PARAMETER },
          }),
        }),
      );
      expect(status).toBe(5);
      expect(output).toMatch(/UNLISTED\s+unknown probe Probe\.ImageId/);
    },
  );

  it("lists an own dynamic reference with a pseudo parameter in its name as own", () => {
    const template = {
      Parameters: { ...bootstrap },
      Resources: {
        Fn: {
          Type: "AWS::Lambda::Function",
          Properties: {
            Environment: {
              Variables: {
                API: { "Fn::Sub": "{{resolve:ssm:/coa/${AWS::Region}/api}}" },
              },
            },
          },
        },
      },
    };
    const { status, output } = check(
      assembly({ "api.template.json": template }),
    );
    expect(status).toBe(0);
    expect(output).toMatch(
      /ok\s+own\s+api Fn\.Environment <- \/coa\/\$\{AWS::Region\}\/api/,
    );
  });

  it("refuses an SSM value anywhere in a launch configuration (every property replaces it)", () => {
    const template = {
      Parameters: { ...bootstrap, ...amiParameter },
      Resources: {
        Lc: {
          Type: "AWS::AutoScaling::LaunchConfiguration",
          Properties: { ImageId: { Ref: "AmiParam" } },
        },
      },
    };
    expect(check(assembly({ "lc.template.json": template })).status).toBe(5);
  });

  it("reads a Fn::Sub variable from its own map, not from an SSM parameter of the same name", () => {
    const template = instanceStack(
      { "Fn::Sub": ["${Ami}", { Ami: "ami-0b712eec697bc409b" }] },
      {
        Ami: {
          Type: "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
          Default: AMI_PARAMETER,
        },
      },
    );
    const { status, output } = check(
      assembly({ "probe.template.json": template }),
    );
    expect(output).not.toMatch(/REFUSE/);
    expect(status).toBe(0);
  });

  it("passes a pinned AMI id, and lists the SSM references that only update in place", () => {
    const template = {
      Parameters: {
        ...bootstrap,
        Issuer: {
          Type: "AWS::SSM::Parameter::Value<String>",
          Default: "/coa/issuer",
        },
      },
      Resources: {
        Probe: {
          Type: "AWS::EC2::Instance",
          Properties: { ImageId: "ami-0b712eec697bc409b" },
        },
        Fn: {
          Type: "AWS::Lambda::Function",
          Properties: {
            Environment: {
              Variables: {
                ISSUER: { Ref: "Issuer" },
                API: "{{resolve:ssm:/coa/metric/api-fn-arn}}",
              },
            },
          },
        },
      },
    };
    const { status, output } = check(
      assembly({ "api.template.json": template }),
    );
    expect(status).toBe(0);
    expect(output).toMatch(/api.*Fn.*Environment.*\/coa\/issuer/);
    expect(output).toMatch(/api.*Fn.*Environment.*\/coa\/metric\/api-fn-arn/);
    expect(output).not.toMatch(/REFUSE/);
  });

  it("lists, without refusing, a replacing property fed by the app's own parameter (it changes only when our stacks write it)", () => {
    const template = {
      Parameters: {
        ...bootstrap,
        MetricFn: {
          Type: "AWS::SSM::Parameter::Value<String>",
          Default: "/coa/metric/api-fn-arn",
        },
      },
      Resources: {
        Invoke: {
          Type: "AWS::Lambda::Permission",
          Properties: {
            FunctionName: { Ref: "MetricFn" },
            Action: "lambda:InvokeFunction",
            Principal: "apigateway.amazonaws.com",
          },
        },
      },
    };
    const { status, output } = check(
      assembly({ "api.template.json": template }),
    );
    expect(status).toBe(0);
    expect(output).toMatch(
      /replaces own\s+api Invoke.FunctionName <- \/coa\/metric\/api-fn-arn/,
    );
    expect(output).not.toMatch(/REFUSE/);
  });

  it("does not count CDK's BootstrapVersion parameter, which no resource uses", () => {
    const { status, output } = check(
      assembly({ "s.template.json": instanceStack("ami-0123abcd") }),
    );
    expect(status).toBe(0);
    expect(output).not.toContain("/cdk-bootstrap/");
  });

  it("marks public parameters (/aws/service/...) as public in the listing", () => {
    const template = {
      Parameters: { ...bootstrap, ...amiParameter },
      Resources: {
        Fn: {
          Type: "AWS::Lambda::Function",
          Properties: {
            Environment: { Variables: { AMI: { Ref: "AmiParam" } } },
          },
        },
      },
    };
    const { status, output } = check(
      assembly({ "fn.template.json": template }),
    );
    expect(status).toBe(0);
    expect(output).toMatch(/ok\s+public fn Fn.Environment <- \/aws\/service\//);
  });

  // A type or property not known to update in place is refused, public or
  // own, until someone checks the CloudFormation reference and lists it.
  it.each([
    [
      "a public AMI in a launch template",
      "AWS::EC2::LaunchTemplate",
      "LaunchTemplateData",
      { ImageId: { Ref: "AmiParam" } },
    ],
    [
      "an own value on an unlisted type",
      "AWS::SNS::Topic",
      "TopicName",
      { Ref: "Own" },
    ],
    [
      "an own value on an unlisted property of a listed type",
      "AWS::Lambda::Function",
      "Runtime",
      { Ref: "Own" },
    ],
  ])(
    "refuses %s (unlisted, so not known to update in place)",
    (_, type, prop, value) => {
      const template = {
        Parameters: {
          ...bootstrap,
          ...amiParameter,
          Own: {
            Type: "AWS::SSM::Parameter::Value<String>",
            Default: "/coa/own",
          },
        },
        Resources: { R: { Type: type, Properties: { [prop]: value } } },
      };
      const { status, output } = check(
        assembly({ "s.template.json": template }),
      );
      expect(status).toBe(5);
      expect(output).toMatch(new RegExp(`UNLISTED.* s R\\.${prop} <- `));
    },
  );

  it("refuses a public ECS-optimized AMI parameter in a task definition (every property replaces it)", () => {
    const template = {
      Parameters: {
        ...bootstrap,
        EcsAmi: {
          Type: "AWS::SSM::Parameter::Value<String>",
          Default:
            "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id",
        },
      },
      Resources: {
        Td: {
          Type: "AWS::ECS::TaskDefinition",
          Properties: {
            ContainerDefinitions: [
              { Environment: [{ Name: "AMI", Value: { Ref: "EcsAmi" } }] },
            ],
          },
        },
      },
    };
    const { status, output } = check(
      assembly({ "td.template.json": template }),
    );
    expect(status).toBe(5);
    expect(output).toMatch(/REFUSE\s+public td Td.ContainerDefinitions/);
  });

  // The serve-on assembly feeds /coa/* into three more pairs, each
  // "Update requires: No interruption" in the CloudFormation Template Reference.
  it.each([
    ["AWS::BedrockAgentCore::Runtime", "AuthorizerConfiguration"],
    ["AWS::BedrockAgentCore::Runtime", "EnvironmentVariables"],
    ["AWS::IAM::ManagedPolicy", "PolicyDocument"],
  ])(
    "passes an own value in %s.%s (serve on, No interruption)",
    (type, prop) => {
      const template = {
        Parameters: {
          ...bootstrap,
          Own: {
            Type: "AWS::SSM::Parameter::Value<String>",
            Default: "/coa/own",
          },
        },
        Resources: {
          R: { Type: type, Properties: { [prop]: { Ref: "Own" } } },
        },
      };
      const { status, output } = check(
        assembly({ "serve.template.json": template }),
      );
      expect(status).toBe(0);
      expect(output).toMatch(
        new RegExp(`^ok\\s+own\\s+serve R\\.${prop} <- /coa/own`, "m"),
      );
    },
  );

  it.each([
    ["AWS::BedrockAgentCore::Runtime", "AgentRuntimeName"],
    ["AWS::IAM::ManagedPolicy", "ManagedPolicyName"],
    ["AWS::IAM::ManagedPolicy", "Description"],
  ])(
    "still refuses an own value in %s.%s (Update requires: Replacement)",
    (type, prop) => {
      const template = {
        Parameters: {
          ...bootstrap,
          Own: {
            Type: "AWS::SSM::Parameter::Value<String>",
            Default: "/coa/own",
          },
        },
        Resources: {
          R: { Type: type, Properties: { [prop]: { Ref: "Own" } } },
        },
      };
      expect(check(assembly({ "serve.template.json": template })).status).toBe(
        5,
      );
    },
  );

  it("passes every deployed stack type and property that uses its own parameters", () => {
    const uses: Array<[string, string]> = [
      ["AWS::ApiGateway::RestApi", "Body"],
      ["AWS::CloudFront::ResponseHeadersPolicy", "ResponseHeadersPolicyConfig"],
      ["AWS::ECS::TaskDefinition", "ContainerDefinitions"],
      ["AWS::IAM::Policy", "PolicyDocument"],
      ["AWS::Lambda::Function", "Environment"],
      ["AWS::Lambda::Permission", "FunctionName"],
      ["AWS::OpenSearchServerless::AccessPolicy", "Policy"],
      ["AWS::StepFunctions::StateMachine", "DefinitionString"],
      ["Custom::AWS", "Create"],
      ["Custom::AWS", "Update"],
      ["Custom::CDKBucketDeployment", "SourceMarkers"],
    ];
    const template = {
      Parameters: {
        ...bootstrap,
        Own: {
          Type: "AWS::SSM::Parameter::Value<String>",
          Default: "/coa/own",
        },
      },
      Resources: Object.fromEntries(
        uses.map(([type, prop], i) => [
          `R${i}`,
          { Type: type, Properties: { [prop]: { Ref: "Own" } } },
        ]),
      ),
    };
    const { status, output } = check(
      assembly({ "own.template.json": template }),
    );
    expect(status).toBe(0);
    expect(output).not.toMatch(/REFUSE|UNLISTED/);
    expect(output.trim().split("\n")).toHaveLength(uses.length);
  });

  it("fails on an empty or missing assembly instead of passing it", () => {
    expect(
      check(fs.mkdtempSync(path.join(os.tmpdir(), "cdkout-"))).status,
    ).not.toBe(0);
    expect(check(path.join(os.tmpdir(), "no-such-cdkout-dir")).status).not.toBe(
      0,
    );
  });
});
