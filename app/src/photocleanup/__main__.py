from photocleanup.app import main

if __name__ == "__main__":
    from photocleanup.diagnostics import get_logger, log_environment, setup_logging
    setup_logging()
    log_environment()
    try:
        main().main_loop()
    except BaseException:
        get_logger().exception("Fatal error in main loop")
        raise
