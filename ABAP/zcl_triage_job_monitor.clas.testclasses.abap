*"* ABAP Triage MVP - collect-only tests (nothing is sent to xMatters).
*"* Precondition: ZCL_TRIAGE_RUNNER tests have run, so ZTRIAGE_TEST_* jobs exist.
CLASS ltc_collect DEFINITION FINAL FOR TESTING
  DURATION MEDIUM
  RISK LEVEL HARMLESS.

  PRIVATE SECTION.
    DATA mo_cut TYPE REF TO zcl_triage_job_monitor.

    METHODS setup.
    METHODS latest
      IMPORTING iv_job          TYPE btcjob
      RETURNING VALUE(rs_alert) TYPE zcl_triage_job_monitor=>ty_alert.
    METHODS div0_is_dump FOR TESTING.
    METHODS data_is_message FOR TESTING.
    METHODS rfc_is_dump FOR TESTING.
    METHODS ok_job_not_collected FOR TESTING.
    METHODS runner_jobs_excluded FOR TESTING.
    METHODS json_has_fields FOR TESTING.
ENDCLASS.

CLASS ltc_collect IMPLEMENTATION.
  METHOD setup.
    mo_cut = NEW #( iv_days = 7 ).
  ENDMETHOD.

  METHOD latest.
    DATA(lt_alert) = mo_cut->collect( it_jobname     = VALUE #( ( sign = 'I' option = 'EQ' low = iv_job ) )
                                      iv_ignore_sent = abap_true ).
    cl_abap_unit_assert=>assert_not_initial( act = lt_alert msg = |No cancelled job { iv_job } found| ).
    rs_alert = lt_alert[ lines( lt_alert ) ].
  ENDMETHOD.

  METHOD div0_is_dump.
    DATA(ls) = latest( 'ZTRIAGE_TEST_DIV0' ).
    cl_abap_unit_assert=>assert_equals( act = ls-error_type exp = 'DUMP' ).
    cl_abap_unit_assert=>assert_char_cp( act = ls-runtime_error exp = '*ZERODIVIDE*' ).
    cl_abap_unit_assert=>assert_equals( act = ls-program exp = 'ZTRIAGE_DEMO_BUG' ).
    cl_abap_unit_assert=>assert_equals( act = ls-variant exp = 'ZBUG_DIV0' ).
    cl_abap_unit_assert=>assert_not_initial( act = ls-error_line ).
    cl_abap_unit_assert=>assert_not_initial( act = ls-joblog ).
  ENDMETHOD.

  METHOD data_is_message.
    DATA(ls) = latest( 'ZTRIAGE_TEST_DATA' ).
    cl_abap_unit_assert=>assert_equals( act = ls-error_type exp = 'MESSAGE' ).
    cl_abap_unit_assert=>assert_char_cp( act = ls-error_message exp = '*does not exist*' ).
    cl_abap_unit_assert=>assert_equals( act = ls-variant exp = 'ZBUG_DATA' ).
  ENDMETHOD.

  METHOD rfc_is_dump.
    DATA(ls) = latest( 'ZTRIAGE_TEST_RFC' ).
    cl_abap_unit_assert=>assert_equals( act = ls-error_type exp = 'DUMP' ).
    cl_abap_unit_assert=>assert_char_cp( act = ls-runtime_error exp = 'CALL_FUNCTION*' ).
  ENDMETHOD.

  METHOD ok_job_not_collected.
    DATA(lt_alert) = mo_cut->collect( it_jobname     = VALUE #( ( sign = 'I' option = 'EQ' low = 'ZTRIAGE_TEST_OK' ) )
                                      iv_ignore_sent = abap_true ).
    cl_abap_unit_assert=>assert_initial( act = lt_alert msg = 'Finished job must not be reported' ).
  ENDMETHOD.

  METHOD runner_jobs_excluded.
    DATA(lt_alert) = mo_cut->collect( it_jobname     = VALUE #( ( sign = 'I' option = 'CP' low = 'ZTRIAGE_RUN_*' ) )
                                      iv_ignore_sent = abap_true ).
    cl_abap_unit_assert=>assert_initial( act = lt_alert msg = 'Fix-agent reproduce jobs must not be reported' ).
  ENDMETHOD.

  METHOD json_has_fields.
    DATA(lv_json) = mo_cut->to_json( latest( 'ZTRIAGE_TEST_DIV0' ) ).
    LOOP AT VALUE string_table( ( `"alert_id":` ) ( `"program":"ZTRIAGE_DEMO_BUG"` ) ( `"variant":"ZBUG_DIV0"` )
                                ( `"runtime_error":` ) ( `"error_type":"DUMP"` ) ( `"joblog":[` ) ) INTO DATA(lv_key).
      cl_abap_unit_assert=>assert_true( act = xsdbool( lv_json CS lv_key ) msg = |JSON misses { lv_key }: { lv_json }| ).
    ENDLOOP.
    cl_abap_unit_assert=>fail( msg = 'INFO sample payload' detail = lv_json
                               level = if_aunit_constants=>tolerable quit = if_aunit_constants=>no ).
  ENDMETHOD.
ENDCLASS.

"! Runs report ZTRIAGE_JOB_MONITOR as background job in TEST mode (no HTTP call).
CLASS ltc_report DEFINITION FINAL FOR TESTING
  DURATION MEDIUM
  RISK LEVEL HARMLESS.
  PRIVATE SECTION.
    METHODS report_test_mode FOR TESTING.
ENDCLASS.

CLASS ltc_report IMPLEMENTATION.
  METHOD report_test_mode.
    DATA(ls) = NEW zcl_triage_runner( )->run_job(
      iv_program = 'ZTRIAGE_JOB_MONITOR'
      it_params  = VALUE #( ( selname = 'S_JOB'  kind = 'S' sign = 'I' option = 'CP' low = 'ZTRIAGE_TEST_*' )
                            ( selname = 'P_DEST' kind = 'P' low = 'ZTRIAGE_XMATTERS' )
                            ( selname = 'P_DAYS' kind = 'P' low = '7' )
                            ( selname = 'P_TEST' kind = 'P' low = 'X' ) ) ).
    DATA(lv_spool) = concat_lines_of( table = ls-spool sep = | | ).
    cl_abap_unit_assert=>assert_equals( act = ls-status exp = 'F' msg = ls-summary ).
    cl_abap_unit_assert=>assert_char_cp( act = lv_spool exp = '*TEST MODE*' msg = lv_spool ).
    cl_abap_unit_assert=>assert_char_cp( act = lv_spool exp = '*ZTRIAGE_TEST_DIV0*' msg = lv_spool ).
    cl_abap_unit_assert=>fail( msg = 'INFO report output' detail = lv_spool
                               level = if_aunit_constants=>tolerable quit = if_aunit_constants=>no ).
  ENDMETHOD.
ENDCLASS.
