"""Job scheduling: definitions, persisted runs, and the polling scheduler.

The package initializer stays empty. The shared task store imports
``contracts`` while the runner imports the agent, and loading either from
here would cycle through persistence.
"""
