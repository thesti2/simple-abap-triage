*"* ABAP Triage MVP - executes ZTRIAGE_DEMO_BUG variants as real background jobs.
*"* Job names ZTRIAGE_TEST_* are intentionally picked up by ZTRIAGE_JOB_MONITOR.
*"* Each test reports its result as a TOLERABLE message (visible in the unit result).
CLASS ltc_demo_jobs DEFINITION FINAL FOR TESTING
  DURATION MEDIUM
  RISK LEVEL HARMLESS.

  PRIVATE SECTION.
    CONSTANTS c_prog TYPE progname VALUE 'ZTRIAGE_DEMO_BUG'.
    DATA mo_cut TYPE REF TO zcl_triage_runner.

    CLASS-METHODS class_setup.
    METHODS setup.
    METHODS run_ok FOR TESTING.
    METHODS run_div0 FOR TESTING.
    METHODS run_data FOR TESTING.
    METHODS run_rfc FOR TESTING.
    METHODS report IMPORTING is_result TYPE zcl_triage_runner=>ty_result.
ENDCLASS.

CLASS ltc_demo_jobs IMPLEMENTATION.
  METHOD class_setup.
    DATA(lo_runner) = NEW zcl_triage_runner( ).
    lo_runner->save_variant(
      iv_program = c_prog iv_variant = 'ZOK' iv_text = 'Triage demo: normal run'
      it_params  = VALUE #( ( selname = 'P_MODE'  kind = 'P' low = 'C' )
                            ( selname = 'P_TOTAL' kind = 'P' low = '1000' )
                            ( selname = 'P_QTY'   kind = 'P' low = '10' ) ) ).
    lo_runner->save_variant(
      iv_program = c_prog iv_variant = 'ZBUG_DIV0' iv_text = 'Triage demo: division by zero'
      it_params  = VALUE #( ( selname = 'P_MODE'  kind = 'P' low = 'C' )
                            ( selname = 'P_TOTAL' kind = 'P' low = '1000' )
                            ( selname = 'P_QTY'   kind = 'P' low = '0' ) ) ).
    lo_runner->save_variant(
      iv_program = c_prog iv_variant = 'ZBUG_DATA' iv_text = 'Triage demo: missing material'
      it_params  = VALUE #( ( selname = 'P_MODE'  kind = 'P' low = 'M' )
                            ( selname = 'P_MATNR' kind = 'P' low = 'ZTRIAGE_NO_MAT' ) ) ).
    lo_runner->save_variant(
      iv_program = c_prog iv_variant = 'ZBUG_RFC' iv_text = 'Triage demo: RFC dest missing'
      it_params  = VALUE #( ( selname = 'P_MODE'  kind = 'P' low = 'R' )
                            ( selname = 'P_DEST'  kind = 'P' low = 'ZTRIAGE_DUMMY' ) ) ).
    COMMIT WORK AND WAIT.
  ENDMETHOD.

  METHOD setup.
    mo_cut = NEW #( ).
  ENDMETHOD.

  METHOD run_ok.
    DATA(ls) = mo_cut->run_job( iv_program = c_prog iv_variant = 'ZOK' iv_jobname = 'ZTRIAGE_TEST_OK' ).
    report( ls ).
    cl_abap_unit_assert=>assert_equals( act = ls-status exp = 'F' msg = ls-summary ).
  ENDMETHOD.

  METHOD run_div0.
    DATA(ls) = mo_cut->run_job( iv_program = c_prog iv_variant = 'ZBUG_DIV0' iv_jobname = 'ZTRIAGE_TEST_DIV0' ).
    report( ls ).
    cl_abap_unit_assert=>assert_equals( act = ls-status exp = 'A' msg = ls-summary ).
    cl_abap_unit_assert=>assert_char_cp( act = ls-dump-runtime_error exp = '*ZERODIVIDE*' msg = ls-summary ).
    " job step must carry the real variant name (not a temporary &0000... copy)
    cl_abap_unit_assert=>assert_char_cp( act = concat_lines_of( ls-joblog ) exp = '*variant ZBUG_DIV0*'
                                         msg = |Variant not kept in job step| ).
  ENDMETHOD.

  METHOD run_data.
    DATA(ls) = mo_cut->run_job( iv_program = c_prog iv_variant = 'ZBUG_DATA' iv_jobname = 'ZTRIAGE_TEST_DATA' ).
    report( ls ).
    cl_abap_unit_assert=>assert_equals( act = ls-status exp = 'A' msg = ls-summary ).
  ENDMETHOD.

  METHOD run_rfc.
    DATA(ls) = mo_cut->run_job( iv_program = c_prog iv_variant = 'ZBUG_RFC' iv_jobname = 'ZTRIAGE_TEST_RFC' ).
    report( ls ).
    cl_abap_unit_assert=>assert_equals( act = ls-status exp = 'A' msg = ls-summary ).
  ENDMETHOD.

  METHOD report.
    DATA(lv_detail) = |JOBLOG:\n{ concat_lines_of( table = is_result-joblog sep = |\n| ) }\n| &&
                      |SPOOL:\n{ concat_lines_of( table = is_result-spool sep = |\n| ) }\n| &&
                      |DUMP: { is_result-dump-runtime_error } { is_result-dump-program } { is_result-dump-include } line { is_result-dump-line }|.
    cl_abap_unit_assert=>fail( msg    = |INFO { is_result-summary }|
                               detail = lv_detail
                               level  = if_aunit_constants=>tolerable
                               quit   = if_aunit_constants=>no ).
  ENDMETHOD.
ENDCLASS.

"! Z_TRIAGE_TR_RELEASE - negative/safe cases only (nothing gets released).
CLASS ltc_tr_release DEFINITION FINAL FOR TESTING
  DURATION SHORT
  RISK LEVEL HARMLESS.
  PRIVATE SECTION.
    METHODS call
      IMPORTING iv_trkorr   TYPE trkorr
      EXPORTING ev_released TYPE flag
                ev_text     TYPE string.
    METHODS unknown_tr FOR TESTING.
    METHODS task_rejected FOR TESTING.
    METHODS already_released FOR TESTING.
ENDCLASS.

CLASS ltc_tr_release IMPLEMENTATION.
  METHOD call.
    DATA lt_msg TYPE bapiret2_t.
    CALL FUNCTION 'Z_TRIAGE_TR_RELEASE'
      EXPORTING iv_trkorr   = iv_trkorr
      IMPORTING ev_released = ev_released
                et_messages = lt_msg.
    ev_text = concat_lines_of( table = VALUE string_table( FOR m IN lt_msg ( |{ m-type }: { m-message }| ) ) sep = `; ` ).
  ENDMETHOD.

  METHOD unknown_tr.
    call( EXPORTING iv_trkorr = 'DERKXXXXXX' IMPORTING ev_released = DATA(lv_rel) ev_text = DATA(lv_text) ).
    cl_abap_unit_assert=>assert_initial( act = lv_rel ).
    cl_abap_unit_assert=>assert_char_cp( act = lv_text exp = 'E:*does not exist*' ).
  ENDMETHOD.

  METHOD task_rejected.
    " any task (has a parent request) of the current user - no hard-coded TR number
    SELECT SINGLE trkorr FROM e070
      WHERE strkorr <> @space AND as4user = @sy-uname
      INTO @DATA(lv_task).
    IF sy-subrc <> 0.
      cl_abap_unit_assert=>fail( msg = 'No transport task found for current user' level = if_aunit_constants=>tolerable ).
    ENDIF.
    call( EXPORTING iv_trkorr = lv_task IMPORTING ev_released = DATA(lv_rel) ev_text = DATA(lv_text) ).
    cl_abap_unit_assert=>assert_initial( act = lv_rel ).
    cl_abap_unit_assert=>assert_char_cp( act = lv_text exp = 'E:*is a task of*' ).
  ENDMETHOD.

  METHOD already_released.
    SELECT SINGLE trkorr FROM e070
      WHERE trstatus = 'R' AND strkorr = @space AND trfunction = 'K'
      INTO @DATA(lv_tr).
    call( EXPORTING iv_trkorr = lv_tr IMPORTING ev_released = DATA(lv_rel) ev_text = DATA(lv_text) ).
    cl_abap_unit_assert=>assert_equals( act = lv_rel exp = abap_true ).
    cl_abap_unit_assert=>assert_char_cp( act = lv_text exp = 'W:*already released*' ).
  ENDMETHOD.
ENDCLASS.
