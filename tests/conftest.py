import os

# Tests must not depend on the developer's .env (environment variables take precedence over it).
os.environ["SERVICE_TOKEN"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["DEBUG"] = "false"
# Never touch a real database from unit tests (test_saved_reports_pg.py uses TEST_DATABASE_URL on purpose).
os.environ["DATABASE_URL"] = ""
