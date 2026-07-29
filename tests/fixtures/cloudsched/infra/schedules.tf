resource "aws_cloudwatch_event_rule" "nightly_rollup" {
  name                = "nightly-rollup"
  schedule_expression = "cron(0 2 * * ? *)"
}

resource "aws_cloudwatch_event_target" "rollup_target" {
  rule = aws_cloudwatch_event_rule.nightly_rollup.name
  arn  = aws_lambda_function.rollup.arn
}

resource "aws_lambda_function" "rollup" {
  function_name = "rollup"
  handler       = "jobs.rollup_daily"
  runtime       = "python3.12"
}

resource "google_cloud_scheduler_job" "gcp_digest" {
  name     = "daily-digest"
  schedule = "0 8 * * *"

  http_target {
    uri = "https://svc/internal/digest"
  }
}
