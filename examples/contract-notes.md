Typical start command:

./labeeb_controller.py --config ./config.toml start \
  --intent-file ./examples/intent.md \
  --workspace /path/to/repo \
  --repo OWNER/REPO \
  --branch feature-branch \
  --risk architecture \
  --allow-path api/app \
  --allow-path api/tests \
  --validate 'docker compose exec -T api php artisan test tests/Feature/TargetTest.php' \
  --preauthorize-plan \
  --background
