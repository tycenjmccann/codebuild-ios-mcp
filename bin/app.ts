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
});

app.synth();
