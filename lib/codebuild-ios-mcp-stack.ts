import * as fs from 'fs';
import * as path from 'path';
import * as yaml from 'js-yaml';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import {
  aws_codebuild as codebuild,
  aws_ec2 as ec2,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_logs as logs,
  aws_s3 as s3,
  aws_s3_deployment as s3deploy,
} from 'aws-cdk-lib';

export interface CodebuildIosMcpStackProps extends cdk.StackProps {
  /** GITHUB source location for the iOS repo CodeBuild builds and tests. */
  readonly githubRepo: string;
  /** Default branch/SHA CodeBuild checks out when none is supplied. */
  readonly sourceVersion: string;
  /** Subdir holding the .xcworkspace/.xcodeproj (maps to PROJECT_DIR). */
  readonly projectDir: string;
  /** Default simulator device name (informational; ios_test can override). */
  readonly defaultDevice: string;
  /**
   * Number of always-on reserved Macs = concurrent build slots. Builds beyond
   * this queue (overflow is QUEUE; ON_DEMAND is unavailable for MAC_ARM). Each
   * instance bills continuously (~$25-30/day), so size to PEAK concurrency, not
   * team headcount. Default 1 (sequential).
   */
  readonly baseCapacity: number;
  /**
   * Also provision a LARGE MAC_ARM fleet (Apple M2 32 GB / 12 vCPU) alongside the
   * default MEDIUM (24 GB / 8 vCPU). Callers route per build via
   * ios_test(compute_size:"large"). When false, only MEDIUM exists and a large
   * request returns a graceful error.
   */
  readonly enableLarge: boolean;
  /** Concurrent build slots on the LARGE fleet (ignored when enableLarge=false). */
  readonly largeBaseCapacity: number;
  /** Days before objects under builds/ expire in the artifacts bucket. */
  readonly artifactRetentionDays: number;
  /** TTL (seconds) for presigned artifact URLs returned by the Lambda. */
  readonly presignTtlSec: number;
  /**
   * Optional VPC wiring so builds can reach private resources (e.g. an internal
   * Nexus repo at build time, internal validation services at test time). When
   * vpcId + at least one subnet are provided, the fleet is attached to the VPC
   * and given a fleet service role with the ENI permissions CodeBuild needs.
   * Leave empty for the default (no VPC, public egress only).
   */
  readonly vpcId?: string;
  readonly subnetIds?: string[];
  readonly securityGroupIds?: string[];
  /**
   * When VPC mode is on, also create the VPC endpoints a private (no-NAT) subnet
   * needs so builds can still reach AWS: an S3 gateway endpoint (artifact upload)
   * plus CloudWatch Logs and CodeBuild interface endpoints (log streaming + API).
   * Set false if your VPC already has a NAT gateway or these endpoints. Ignored
   * when VPC mode is off. Default true.
   */
  readonly createVpcEndpoints?: boolean;
}

/**
 * codebuild-ios-mcp
 *
 * Provisions the full AWS surface for the iOS build+test MCP toolset:
 *   - S3 artifacts bucket (private, lifecycle-expired, seeded with the xcresult converter)
 *   - A reserved MAC_ARM CodeBuild fleet (CfnFleet; no L2 construct exists)
 *   - A CodeBuild project that references the fleet and embeds buildspec.yaml inline
 *   - A python3.12 Lambda hosting the four MCP tools
 *   - An IAM role the AgentCore Gateway assumes to invoke the Lambda
 *
 * The Bedrock AgentCore Gateway and its target are NOT modeled here: no
 * CloudFormation resource exists for them yet. scripts/register-gateway.sh wires
 * those up from the stack outputs as a one-time CLI step.
 */
export class CodebuildIosMcpStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: CodebuildIosMcpStackProps) {
    super(scope, id, props);

    const region = cdk.Stack.of(this).region;

    // ----------------------------------------------------------------------- //
    // Naming. Account-suffixed bucket name keeps it globally unique without
    // hardcoding the account id (derived from Stack.of(this).account).
    // ----------------------------------------------------------------------- //
    const projectName = 'ios-agent-tests';
    const fleetName = 'ios-agent-tests-mac-medium';
    const fleetNameLarge = 'ios-agent-tests-mac-large';
    const reportGroupName = `${projectName}-ios-test-report`;
    const lambdaName = 'codebuild-ios-mcp';
    const bucketName = `ios-agent-test-artifacts-${this.account}`;

    // ----------------------------------------------------------------------- //
    // S3 artifacts bucket: block all public access, expire builds/ artifacts,
    // encrypt at rest, enforce SSL. Seeded with the xcresult->JUnit converter.
    // ----------------------------------------------------------------------- //
    const artifactsBucket = new s3.Bucket(this, 'ArtifactsBucket', {
      bucketName,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [
        {
          id: 'expire-artifacts',
          enabled: true,
          prefix: 'builds/',
          expiration: cdk.Duration.days(props.artifactRetentionDays),
        },
        {
          // Warm-build cache (DerivedData + SPM + stable src) is overwritten on
          // every build under a per-repo key, so this only reaps tarballs for
          // repos that stopped building. 30d of inactivity = safe to drop (next
          // build just recompiles cold once and reseeds).
          id: 'expire-stale-warm-cache',
          enabled: true,
          prefix: 'warm-cache/',
          expiration: cdk.Duration.days(30),
        },
      ],
    });

    // The buildspec fetches s3://<bucket>/tooling/xcresult_to_junit.py at runtime.
    new s3deploy.BucketDeployment(this, 'ToolingDeployment', {
      destinationBucket: artifactsBucket,
      destinationKeyPrefix: 'tooling',
      sources: [s3deploy.Source.asset(path.join(__dirname, '..', 'tooling'))],
      prune: false,
      retainOnDelete: false,
    });

    // ----------------------------------------------------------------------- //
    // CodeBuild Test Report group (JUNITXML). Importing by name gives us the
    // ARN to scope IAM policies precisely; the project creates it on first run.
    // ----------------------------------------------------------------------- //
    const reportGroupArn = cdk.Stack.of(this).formatArn({
      service: 'codebuild',
      resource: 'report-group',
      resourceName: reportGroupName,
    });

    // ----------------------------------------------------------------------- //
    // MAC_ARM reserved CodeBuild fleet. No L2/L1 construct exists, so use the
    // raw CloudFormation resource. ON_DEMAND overflow is NOT supported for
    // MAC_ARM, hence QUEUE.
    // ----------------------------------------------------------------------- //
    // Optional VPC: attach the fleet to private subnets so builds reach internal
    // resources (Nexus, internal validation services). Enabled only when a VPC id
    // and at least one subnet are supplied. CodeBuild requires a fleet service
    // role (trusts codebuild.amazonaws.com) with ENI permissions when in a VPC.
    const subnetIds = props.subnetIds ?? [];
    const vpcEnabled = Boolean(props.vpcId) && subnetIds.length > 0;

    let fleetServiceRoleArn: string | undefined;
    let fleetVpcConfig: codebuild.CfnFleet.VpcConfigProperty | undefined;
    if (vpcEnabled) {
      const fleetServiceRole = new iam.Role(this, 'FleetServiceRole', {
        roleName: `${fleetName}-fleet-role`,
        assumedBy: new iam.ServicePrincipal('codebuild.amazonaws.com'),
        description: 'Fleet service role granting CodeBuild the ENI permissions to run builds inside the VPC.',
      });
      // Scoped to ENI lifecycle in the configured subnets; CreateNetworkInterfacePermission
      // is guarded by the codebuild.amazonaws.com authorized service condition.
      fleetServiceRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'VpcEni',
          actions: [
            'ec2:CreateNetworkInterface',
            'ec2:DescribeNetworkInterfaces',
            'ec2:DeleteNetworkInterface',
            'ec2:DescribeSubnets',
            'ec2:DescribeSecurityGroups',
            'ec2:DescribeDhcpOptions',
            'ec2:DescribeVpcs',
          ],
          resources: ['*'],
        }),
      );
      fleetServiceRole.addToPolicy(
        new iam.PolicyStatement({
          sid: 'VpcEniPermission',
          actions: ['ec2:CreateNetworkInterfacePermission'],
          resources: [`arn:${this.partition}:ec2:${region}:${this.account}:network-interface/*`],
          conditions: {
            StringEquals: { 'ec2:AuthorizedService': 'codebuild.amazonaws.com' },
          },
        }),
      );
      fleetServiceRoleArn = fleetServiceRole.roleArn;
      fleetVpcConfig = {
        vpcId: props.vpcId,
        subnets: subnetIds,
        securityGroupIds: props.securityGroupIds ?? [],
      };

      // VPC endpoints so a private (no-NAT) subnet can still reach AWS: S3
      // gateway (artifact upload) + CloudWatch Logs and CodeBuild interface
      // endpoints (log streaming + control-plane). Without these, a VPC build
      // reaches private hosts but loses access to AWS services. Built from raw
      // ids via L1 resources to avoid Vpc.fromVpcAttributes' AZ/subnet-count
      // constraint (we only have ids, not a full VPC model).
      if (props.createVpcEndpoints !== false) {
        // Interface-endpoint SG: HTTPS in from the build SGs, all out.
        const endpointSg = new ec2.CfnSecurityGroup(this, 'VpcEndpointSg', {
          vpcId: props.vpcId!,
          groupDescription: 'HTTPS from the CodeBuild fleet to interface VPC endpoints.',
          securityGroupIngress: (props.securityGroupIds ?? []).map((sgId) => ({
            ipProtocol: 'tcp',
            fromPort: 443,
            toPort: 443,
            sourceSecurityGroupId: sgId,
            description: 'HTTPS from CodeBuild build security group',
          })),
        });

        // S3 gateway endpoint (free; needs a route table — left to the operator's
        // private route table, association added out-of-band if not default).
        new ec2.CfnVPCEndpoint(this, 'S3GatewayEndpoint', {
          vpcId: props.vpcId!,
          serviceName: `com.amazonaws.${region}.s3`,
          vpcEndpointType: 'Gateway',
        });

        // Interface endpoints for CloudWatch Logs + CodeBuild.
        for (const [id, svc] of [
          ['LogsEndpoint', 'logs'],
          ['CodeBuildEndpoint', 'codebuild'],
        ] as const) {
          new ec2.CfnVPCEndpoint(this, id, {
            vpcId: props.vpcId!,
            serviceName: `com.amazonaws.${region}.${svc}`,
            vpcEndpointType: 'Interface',
            subnetIds,
            securityGroupIds: [endpointSg.attrGroupId],
            privateDnsEnabled: true,
          });
        }
      }
    }

    // Two MAC_ARM fleets so callers can pick a size per build via StartBuild's
    // fleetOverride (a MAC_ARM fleet can't change computeType in place, so size
    // is expressed as separate fleets, not one mutable fleet). The project binds
    // to MEDIUM as its default; LARGE is reached only via per-call fleetOverride
    // in the Lambda. Both share image / env type / overflow / VPC config.
    const fleet = new codebuild.CfnFleet(this, 'MacArmFleet', {
      name: fleetName,
      baseCapacity: props.baseCapacity,
      computeType: 'BUILD_GENERAL1_MEDIUM',
      environmentType: 'MAC_ARM',
      overflowBehavior: 'QUEUE',
      // Only set when VPC is enabled; otherwise left undefined (no VPC).
      fleetServiceRole: fleetServiceRoleArn,
      fleetVpcConfig,
    });

    // Optional LARGE fleet (M2 32GB/12vCPU). Gated by enableLarge so it can be
    // dropped to stop its billing. Reached via compute_size:"large" on ios_test.
    const fleetLarge = props.enableLarge
      ? new codebuild.CfnFleet(this, 'MacArmFleetLarge', {
          name: fleetNameLarge,
          baseCapacity: props.largeBaseCapacity,
          computeType: 'BUILD_GENERAL1_LARGE',
          environmentType: 'MAC_ARM',
          overflowBehavior: 'QUEUE',
          fleetServiceRole: fleetServiceRoleArn,
          fleetVpcConfig,
        })
      : undefined;

    // ----------------------------------------------------------------------- //
    // CodeBuild service role: scoped to its log group, the artifacts bucket,
    // and the report group. Least privilege.
    // ----------------------------------------------------------------------- //
    const codeBuildLogGroup = new logs.LogGroup(this, 'CodeBuildLogGroup', {
      logGroupName: `/aws/codebuild/${projectName}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const codeBuildRole = new iam.Role(this, 'CodeBuildServiceRole', {
      roleName: `${projectName}-codebuild-role`,
      assumedBy: new iam.ServicePrincipal('codebuild.amazonaws.com'),
      description: 'Service role for the codebuild-ios-mcp macOS build project.',
    });

    codeBuildRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'Logs',
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
        resources: [
          codeBuildLogGroup.logGroupArn,
          `${codeBuildLogGroup.logGroupArn}:*`,
        ],
      }),
    );
    artifactsBucket.grantReadWrite(codeBuildRole);
    codeBuildRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'CodeBuildReports',
        actions: [
          'codebuild:CreateReportGroup',
          'codebuild:CreateReport',
          'codebuild:UpdateReport',
          'codebuild:BatchPutTestCases',
          'codebuild:BatchPutCodeCoverages',
        ],
        resources: [reportGroupArn],
      }),
    );

    // ----------------------------------------------------------------------- //
    // CodeBuild project. The buildspec.yaml at the repo root is the SINGLE
    // SOURCE OF TRUTH: it is read at synth time and embedded inline so the
    // user's iOS repo needs no buildspec file. Edit buildspec.yaml + redeploy
    // to update build behavior.
    // ----------------------------------------------------------------------- //
    const buildspecPath = path.join(__dirname, '..', 'buildspec.yaml');
    const buildspecObject = yaml.load(fs.readFileSync(buildspecPath, 'utf8')) as {
      [key: string]: unknown;
    };

    // No CodeBuild cache construct: warm DerivedData + resolved SPM persist in
    // $HOME/ios-mcp-state on the reserved Mac (the instance stays alive between
    // builds). LOCAL_CUSTOM_CACHE symlinks through a root-owned store the build
    // user can't write (POSIX 13); S3 cache adds zip/download overhead unneeded
    // while the instance persists. The buildspec owns warm-state handling.

    const project = new codebuild.Project(this, 'IosTestProject', {
      projectName,
      source: codebuild.Source.gitHub({
        owner: parseGitHubOwner(props.githubRepo),
        repo: parseGitHubRepo(props.githubRepo),
        branchOrRef: props.sourceVersion,
      }),
      buildSpec: codebuild.BuildSpec.fromObjectToYaml(buildspecObject),
      role: codeBuildRole,
      timeout: cdk.Duration.minutes(40),
      environment: {
        // The L2 rejects a Mac image at construct time ("Mac images must be used
        // with a fleet") because it can't see the fleet we attach below via
        // escape hatch. Pass a Linux placeholder purely to clear that validation;
        // the CfnProject overrides Type/Image/ComputeType/Fleet to MAC_ARM next,
        // and those overrides are what CloudFormation actually deploys.
        buildImage: codebuild.LinuxBuildImage.STANDARD_7_0,
        computeType: codebuild.ComputeType.MEDIUM,
      },
      environmentVariables: {
        ARTIFACTS_BUCKET: { value: artifactsBucket.bucketName },
        PROJECT_DIR: { value: props.projectDir },
      },
      logging: {
        cloudWatch: { logGroup: codeBuildLogGroup, enabled: true },
      },
    });

    // Attach the MAC_ARM reserved fleet. The L2 has no fleet prop, so set
    // Environment.Fleet.FleetArn on the underlying CfnProject (escape hatch).
    const cfnProject = project.node.defaultChild as codebuild.CfnProject;
    cfnProject.addPropertyOverride('Environment.Fleet.FleetArn', fleet.attrArn);
    cfnProject.addPropertyOverride('Environment.Type', 'MAC_ARM');
    cfnProject.addPropertyOverride('Environment.Image', 'aws/codebuild/macos-arm-base:14');
    cfnProject.addPropertyOverride('Environment.ComputeType', 'BUILD_GENERAL1_MEDIUM');
    project.node.addDependency(fleet);

    // The Project L2 does not model report groups; the buildspec's
    // `reports: ios-test-report` block makes CodeBuild auto-create
    // <project>-ios-test-report on first run. IAM above already scopes to it.

    // ----------------------------------------------------------------------- //
    // Lambda: hosts the four MCP tools. python3.12, 256MB, 30s, from lambda/.
    // ----------------------------------------------------------------------- //
    const fn = new lambda.Function(this, 'McpToolsFunction', {
      functionName: lambdaName,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '..', 'lambda')),
      memorySize: 256,
      timeout: cdk.Duration.seconds(30),
      description: 'codebuild-ios-mcp tool dispatcher (ios_test, ios_build_status, list_schemes, get_test_logs, get_build_log, ios_cancel).',
      environment: {
        CODEBUILD_PROJECT: project.projectName,
        ARTIFACTS_BUCKET: artifactsBucket.bucketName,
        PRESIGN_TTL_SEC: String(props.presignTtlSec),
        // Fleet ARNs for per-call compute_size routing via StartBuild fleetOverride.
        // MEDIUM is the project default; LARGE empty when the large fleet is off.
        FLEET_MEDIUM_ARN: fleet.attrArn,
        FLEET_LARGE_ARN: fleetLarge ? fleetLarge.attrArn : '',
        // AWS_REGION is reserved/auto-populated by the Lambda runtime.
      },
    });

    // Lambda exec role: least privilege. Structured results come from
    // s3://<bucket>/builds/<id>/summary.json (written by the buildspec), NOT the
    // CodeBuild Test Reports API — so no report-read permissions are needed.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'CodeBuildRunAndRead',
        // StopBuild powers ios_cancel; BatchGetBuilds powers status + phase
        // timeline; ListBuildsForProject powers ios_list_builds (pool/queue view).
        actions: [
          'codebuild:StartBuild',
          'codebuild:BatchGetBuilds',
          'codebuild:StopBuild',
          'codebuild:ListBuildsForProject',
        ],
        resources: [project.projectArn],
      }),
    );
    // Live log tail (get_build_log while IN_PROGRESS) reads the project's
    // CloudWatch log stream directly. Scoped to this project's log group.
    fn.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ReadBuildLogs',
        actions: ['logs:GetLogEvents'],
        resources: [`${codeBuildLogGroup.logGroupArn}:*`],
      }),
    );
    artifactsBucket.grantRead(fn);

    // ----------------------------------------------------------------------- //
    // AgentCore Gateway wiring. No IaC resource exists for the Gateway/target,
    // so we provision only what CloudFormation can:
    //   (a) a role the Gateway assumes to invoke the Lambda
    //   (b) a Lambda resource permission for the AgentCore service principal
    // scripts/register-gateway.sh finishes the job from the outputs below.
    // ----------------------------------------------------------------------- //
    const gatewayInvokeRole = new iam.Role(this, 'GatewayInvokeRole', {
      roleName: 'codebuild-ios-mcp-gateway-invoke-role',
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Role assumed by the Bedrock AgentCore Gateway to invoke the codebuild-ios-mcp Lambda target.',
    });
    gatewayInvokeRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'InvokeMcpLambda',
        actions: ['lambda:InvokeFunction'],
        resources: [fn.functionArn],
      }),
    );

    fn.addPermission('AgentCoreGatewayInvoke', {
      principal: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      action: 'lambda:InvokeFunction',
    });

    // ----------------------------------------------------------------------- //
    // Outputs consumed by scripts/register-gateway.sh and operators.
    // ----------------------------------------------------------------------- //
    new cdk.CfnOutput(this, 'ArtifactsBucketName', {
      value: artifactsBucket.bucketName,
      description: 'S3 bucket holding build artifacts and the xcresult converter.',
    });
    new cdk.CfnOutput(this, 'CodeBuildProjectName', {
      value: project.projectName,
      description: 'CodeBuild project name (pass as sourceVersion target to ios_test).',
    });
    new cdk.CfnOutput(this, 'CodeBuildProjectArn', {
      value: project.projectArn,
      description: 'CodeBuild project ARN.',
    });
    new cdk.CfnOutput(this, 'FleetName', {
      value: fleetName,
      description: 'MAC_ARM reserved fleet name (delete this to stop fleet billing).',
    });
    new cdk.CfnOutput(this, 'FleetArn', {
      value: fleet.attrArn,
      description: 'MAC_ARM reserved fleet ARN (MEDIUM, project default).',
    });
    if (fleetLarge) {
      new cdk.CfnOutput(this, 'FleetArnLarge', {
        value: fleetLarge.attrArn,
        description: 'MAC_ARM reserved fleet ARN (LARGE, via compute_size:large).',
      });
    }
    new cdk.CfnOutput(this, 'LambdaArn', {
      value: fn.functionArn,
      description: 'MCP tools Lambda ARN (the AgentCore Gateway target).',
    });
    new cdk.CfnOutput(this, 'LambdaName', {
      value: fn.functionName,
      description: 'MCP tools Lambda function name.',
    });
    new cdk.CfnOutput(this, 'GatewayInvokeRoleArn', {
      value: gatewayInvokeRole.roleArn,
      description: 'IAM role ARN the AgentCore Gateway assumes (--role-arn for create-gateway).',
    });
    new cdk.CfnOutput(this, 'ReportGroupArn', {
      value: reportGroupArn,
      description: 'CodeBuild Test Report group ARN (JUNITXML).',
    });
    new cdk.CfnOutput(this, 'StackRegion', {
      value: region,
      description: 'Region the stack is deployed in (used by the helper scripts).',
    });
    new cdk.CfnOutput(this, 'FleetVpcStatus', {
      value: vpcEnabled ? `${props.vpcId} (${subnetIds.length} subnet(s))` : 'none (public egress only)',
      description: 'Whether the fleet is attached to a VPC for private resource access.',
    });
  }
}

// --------------------------------------------------------------------------- //
// GitHub URL parsing. Accepts https://github.com/owner/repo[.git] and
// git@github.com:owner/repo[.git].
// --------------------------------------------------------------------------- //
function splitGitHub(url: string): { owner: string; repo: string } {
  const cleaned = url.replace(/\.git$/, '').replace(/\/$/, '');
  const m = cleaned.match(/github\.com[/:]([^/]+)\/([^/]+)$/);
  if (!m) {
    throw new Error(
      `Could not parse a GitHub owner/repo from "${url}". Expected https://github.com/<owner>/<repo>.`,
    );
  }
  return { owner: m[1], repo: m[2] };
}

function parseGitHubOwner(url: string): string {
  return splitGitHub(url).owner;
}

function parseGitHubRepo(url: string): string {
  return splitGitHub(url).repo;
}
