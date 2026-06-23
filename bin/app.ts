#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { CodebuildIosMcpStack } from '../lib/codebuild-ios-mcp-stack';

const app = new cdk.App();

/**
 * Context values (override with `-c key=value` on the CLI or by editing cdk.json):
 *   codebuild-ios-mcp:githubRepo            GITHUB source location for the iOS repo under test
 *   codebuild-ios-mcp:sourceVersion         Default branch/SHA the project checks out
 *   codebuild-ios-mcp:projectDir            Subdir holding the .xcworkspace/.xcodeproj (PROJECT_DIR)
 *   codebuild-ios-mcp:defaultDevice         Default simulator device name
 *   codebuild-ios-mcp:artifactRetentionDays Days before builds/ artifacts expire
 *   codebuild-ios-mcp:presignTtlSec         TTL for presigned artifact URLs
 *
 * Account/region resolve from the standard CDK environment variables populated by
 * the AWS profile in use (CDK_DEFAULT_ACCOUNT / CDK_DEFAULT_REGION). No hardcoding.
 */
function ctx<T>(key: string, fallback: T): T {
  const v = app.node.tryGetContext(`codebuild-ios-mcp:${key}`);
  return v === undefined || v === null ? fallback : (v as T);
}

new CodebuildIosMcpStack(app, 'CodebuildIosMcpStack', {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  description:
    'codebuild-ios-mcp: macOS CodeBuild iOS build+test runner exposed to AI agents as MCP tools via Bedrock AgentCore Gateway.',
  githubRepo: ctx<string>('githubRepo', 'https://github.com/aws-samples/aws-mobile-ios-notes-tutorial'),
  sourceVersion: ctx<string>('sourceVersion', 'main'),
  projectDir: ctx<string>('projectDir', '.'),
  defaultDevice: ctx<string>('defaultDevice', 'iPhone 17'),
  artifactRetentionDays: Number(ctx<number>('artifactRetentionDays', 14)),
  presignTtlSec: Number(ctx<number>('presignTtlSec', 3600)),
  // Optional VPC wiring — populate to reach private resources (Nexus, internal
  // validation services). Empty = no VPC, fleet runs with public egress only.
  vpcId: ctx<string>('vpcId', ''),
  subnetIds: csv(ctx<string>('subnetIds', '')),
  securityGroupIds: csv(ctx<string>('securityGroupIds', '')),
  // Default true: when VPC mode is on, also create S3/Logs/CodeBuild endpoints so
  // a private no-NAT subnet works out of the box. Set false if you have a NAT.
  createVpcEndpoints: bool(ctx<unknown>('createVpcEndpoints', true), true),
  // Build cache to speed the fix->retest loop: 'none' | 'local' | 's3'.
  // Default 'local': warm DerivedData on the reserved Mac so re-tests are
  // incremental. First build is still cold; per-call clean_build forces a cold
  // run when needed. Set 'none' only for always-clean validation.
  cacheMode: cacheMode(ctx<string>('cacheMode', 'local')),
});

/** Validate the cacheMode context value, falling back to 'none'. */
function cacheMode(v: string): 'none' | 'local' | 's3' {
  return v === 'local' || v === 's3' ? v : 'none';
}

/** Parse a comma-separated context string into a trimmed, non-empty array. */
function csv(v: string): string[] {
  return v.split(',').map((s) => s.trim()).filter(Boolean);
}

/** Coerce a context value to boolean. CLI `-c k=false` arrives as the string
 *  "false", which is truthy — treat "false"/"0"/"no" as false. */
function bool(v: unknown, fallback: boolean): boolean {
  if (typeof v === 'boolean') return v;
  if (typeof v === 'string') return !['false', '0', 'no', ''].includes(v.toLowerCase());
  return fallback;
}

app.synth();
