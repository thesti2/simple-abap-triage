"! ABAP Triage MVP - run a program with a variant as background job
"! and collect the result (status, job log, spool output, short dump).
"! Also provides job log / dump helpers reused by ZTRIAGE_JOB_MONITOR.
CLASS zcl_triage_runner DEFINITION
  PUBLIC
  FINAL
  CREATE PUBLIC .

  PUBLIC SECTION.
    TYPES:
      BEGIN OF ty_dump,
        runtime_error TYPE string,
        program       TYPE string,
        include       TYPE string,
        line          TYPE string,
        datum         TYPE d,
        uzeit         TYPE t,
      END OF ty_dump,
      BEGIN OF ty_result,
        jobname  TYPE btcjob,
        jobcount TYPE btcjobcnt,
        status   TYPE btcstatus,  " F = finished, A = cancelled
        joblog   TYPE string_table,
        spool    TYPE string_table,
        dump     TYPE ty_dump,
        summary  TYPE string,
      END OF ty_result.

    CONSTANTS c_run_prefix TYPE string VALUE 'ZTRIAGE_RUN_'.

    "! Submit program (+variant / parameters) as immediate background job and wait for it.
    METHODS run_job
      IMPORTING iv_program       TYPE progname
                iv_variant       TYPE variant OPTIONAL
                it_params        TYPE rsparams_tt OPTIONAL
                iv_jobname       TYPE btcjob OPTIONAL
                iv_wait_sec      TYPE i DEFAULT 120
      RETURNING VALUE(rs_result) TYPE ty_result.

    METHODS read_joblog
      IMPORTING iv_jobname       TYPE btcjob
                iv_jobcount      TYPE btcjobcnt
      RETURNING VALUE(rt_joblog) TYPE string_table.

    METHODS read_spool
      IMPORTING iv_jobname      TYPE btcjob
                iv_jobcount     TYPE btcjobcnt
      RETURNING VALUE(rt_spool) TYPE string_table.

    "! Latest short dump of a user within a time window (job start..end).
    METHODS find_dump
      IMPORTING iv_user        TYPE syuname
                iv_from_date   TYPE d
                iv_from_time   TYPE t
                iv_to_date     TYPE d
                iv_to_time     TYPE t
      RETURNING VALUE(rs_dump) TYPE ty_dump.

    "! Create or overwrite a report variant (setup helper for demo variants).
    METHODS save_variant
      IMPORTING iv_program TYPE progname
                iv_variant TYPE variant
                iv_text    TYPE varit-vtext
                it_params  TYPE rsparams_tt
      RETURNING VALUE(rv_ok) TYPE abap_bool.

  PROTECTED SECTION.
  PRIVATE SECTION.
    METHODS parse_flist
      IMPORTING iv_flist       TYPE csequence
      RETURNING VALUE(rs_dump) TYPE ty_dump.
ENDCLASS.



CLASS zcl_triage_runner IMPLEMENTATION.

  METHOD run_job.
    DATA lv_jobcount TYPE btcjobcnt.

    DATA(lv_jobname) = COND btcjob( WHEN iv_jobname IS NOT INITIAL THEN iv_jobname
                                    ELSE |{ c_run_prefix }{ iv_program }| ).
    rs_result-jobname = lv_jobname.

    CALL FUNCTION 'JOB_OPEN'
      EXPORTING  jobname  = lv_jobname
      IMPORTING  jobcount = lv_jobcount
      EXCEPTIONS OTHERS   = 1.
    IF sy-subrc <> 0.
      rs_result-summary = |JOB_OPEN failed for { lv_jobname }|.
      RETURN.
    ENDIF.
    rs_result-jobcount = lv_jobcount.

    " SUBMIT ... VIA JOB always stores a temporary variant copy (&0000...) in the job step.
    " JOB_SUBMIT keeps the real variant name, like a job scheduled in SM36.
    IF iv_variant IS NOT INITIAL AND it_params IS INITIAL.
      CALL FUNCTION 'JOB_SUBMIT'
        EXPORTING  authcknam = sy-uname
                   jobcount  = lv_jobcount
                   jobname   = lv_jobname
                   report    = iv_program
                   variant   = iv_variant
        EXCEPTIONS OTHERS    = 1.
    ELSEIF iv_variant IS NOT INITIAL.
      SUBMIT (iv_program) USING SELECTION-SET iv_variant
                          WITH SELECTION-TABLE it_params
                          VIA JOB lv_jobname NUMBER lv_jobcount
                          AND RETURN.
    ELSE.
      SUBMIT (iv_program) WITH SELECTION-TABLE it_params
                          VIA JOB lv_jobname NUMBER lv_jobcount
                          AND RETURN.
    ENDIF.
    IF sy-subrc <> 0.
      rs_result-summary = |SUBMIT { iv_program } failed (sy-subrc { sy-subrc })|.
      RETURN.
    ENDIF.

    CALL FUNCTION 'JOB_CLOSE'
      EXPORTING  jobcount  = lv_jobcount
                 jobname   = lv_jobname
                 strtimmed = abap_true
      EXCEPTIONS OTHERS    = 1.
    IF sy-subrc <> 0.
      rs_result-summary = |JOB_CLOSE failed for { lv_jobname }/{ lv_jobcount }|.
      RETURN.
    ENDIF.

    " wait for completion
    DO iv_wait_sec TIMES.
      SELECT SINGLE status FROM tbtco
        WHERE jobname = @lv_jobname AND jobcount = @lv_jobcount
        INTO @rs_result-status.
      IF rs_result-status = 'F' OR rs_result-status = 'A'.
        EXIT.
      ENDIF.
      WAIT UP TO 1 SECONDS.
    ENDDO.

    SELECT SINGLE sdluname, strtdate, strttime, enddate, endtime FROM tbtco
      WHERE jobname = @lv_jobname AND jobcount = @lv_jobcount
      INTO @DATA(ls_job).

    rs_result-joblog = read_joblog( iv_jobname = lv_jobname iv_jobcount = lv_jobcount ).
    rs_result-spool  = read_spool( iv_jobname = lv_jobname iv_jobcount = lv_jobcount ).

    IF rs_result-status = 'A' AND ls_job-strtdate IS NOT INITIAL.
      rs_result-dump = find_dump( iv_user      = ls_job-sdluname
                                  iv_from_date = ls_job-strtdate
                                  iv_from_time = ls_job-strttime
                                  iv_to_date   = COND #( WHEN ls_job-enddate IS INITIAL THEN sy-datum ELSE ls_job-enddate )
                                  iv_to_time   = COND #( WHEN ls_job-endtime IS INITIAL THEN sy-uzeit ELSE ls_job-endtime ) ).
    ENDIF.

    rs_result-summary = |Job { lv_jobname }/{ lv_jobcount } status { rs_result-status }|.
    IF rs_result-dump-runtime_error IS NOT INITIAL.
      rs_result-summary = |{ rs_result-summary }, dump { rs_result-dump-runtime_error } in { rs_result-dump-program } line { rs_result-dump-line }|.
    ENDIF.
  ENDMETHOD.


  METHOD read_joblog.
    DATA lt_log TYPE STANDARD TABLE OF tbtc5.

    CALL FUNCTION 'BP_JOBLOG_READ'
      EXPORTING  client    = sy-mandt
                 jobcount  = iv_jobcount
                 jobname   = iv_jobname
      TABLES     joblogtbl = lt_log
      EXCEPTIONS OTHERS    = 1.
    IF sy-subrc <> 0.
      RETURN.
    ENDIF.

    LOOP AT lt_log INTO DATA(ls_log).
      APPEND |{ ls_log-enterdate DATE = ISO } { ls_log-entertime TIME = ISO } { ls_log-msgtype } { ls_log-msgid }{ ls_log-msgno } { ls_log-text }| TO rt_joblog.
    ENDLOOP.
  ENDMETHOD.


  METHOD read_spool.
    DATA lt_buffer TYPE STANDARD TABLE OF soli.

    SELECT SINGLE listident FROM tbtcp
      WHERE jobname = @iv_jobname AND jobcount = @iv_jobcount AND stepcount = 1
      INTO @DATA(lv_spool).
    IF sy-subrc <> 0 OR lv_spool IS INITIAL.
      RETURN.
    ENDIF.

    DATA(lv_rqident) = CONV tsp01-rqident( lv_spool ).
    CALL FUNCTION 'RSPO_RETURN_ABAP_SPOOLJOB'
      EXPORTING  rqident = lv_rqident
      TABLES     buffer  = lt_buffer
      EXCEPTIONS OTHERS  = 1.
    IF sy-subrc <> 0.
      RETURN.
    ENDIF.

    LOOP AT lt_buffer INTO DATA(ls_buffer).
      APPEND condense( val = CONV string( ls_buffer-line ) del = ` ` ) TO rt_spool.
    ENDLOOP.
  ENDMETHOD.


  METHOD find_dump.
    " SNAP_BEG: first record (seqno 000) of each short dump holds the key fields in FLIST
    SELECT datum, uzeit, flist FROM snap_beg
      WHERE seqno = '000'
        AND mandt = @sy-mandt
        AND uname = @iv_user
        AND datum BETWEEN @iv_from_date AND @iv_to_date
      ORDER BY datum DESCENDING, uzeit DESCENDING
      INTO TABLE @DATA(lt_snap)
      UP TO 50 ROWS.

    LOOP AT lt_snap INTO DATA(ls_snap).
      DATA(lv_ts)   = |{ ls_snap-datum }{ ls_snap-uzeit }|.
      DATA(lv_from) = |{ iv_from_date }{ iv_from_time }|.
      DATA(lv_to)   = |{ iv_to_date }{ iv_to_time }|.
      IF lv_ts >= lv_from AND lv_ts <= lv_to.
        rs_dump = parse_flist( ls_snap-flist ).
        rs_dump-datum = ls_snap-datum.
        rs_dump-uzeit = ls_snap-uzeit.
        RETURN.
      ENDIF.
    ENDLOOP.
  ENDMETHOD.


  METHOD parse_flist.
    " FLIST format: <2-char id><3-digit length><value>... (FC = runtime error,
    " AP = program, AI = include, AL = line)
    DATA lv_len TYPE i.
    DATA(lv_off) = 0.
    DATA(lv_max) = strlen( iv_flist ).

    DATA lv_id    TYPE string.
    DATA lv_len_c TYPE string.
    DATA lv_val   TYPE string.

    WHILE lv_off + 5 <= lv_max.
      lv_id    = substring( val = iv_flist off = lv_off len = 2 ).
      lv_len_c = substring( val = iv_flist off = lv_off + 2 len = 3 ).
      IF lv_len_c CN '0123456789'.
        EXIT.
      ENDIF.
      lv_len = lv_len_c.
      lv_off = lv_off + 5.
      IF lv_off + lv_len > lv_max.
        EXIT.
      ENDIF.
      lv_val = substring( val = iv_flist off = lv_off len = lv_len ).
      CASE lv_id.
        WHEN 'FC'. rs_dump-runtime_error = lv_val.
        WHEN 'AP'. rs_dump-program       = lv_val.
        WHEN 'AI'. rs_dump-include       = lv_val.
        WHEN 'AL'. rs_dump-line          = lv_val.
      ENDCASE.
      lv_off = lv_off + lv_len.
    ENDWHILE.
  ENDMETHOD.


  METHOD save_variant.
    DATA ls_varid TYPE varid.
    DATA lt_text  TYPE STANDARD TABLE OF varit.
    DATA lt_params TYPE STANDARD TABLE OF rsparams.
    DATA lv_rc TYPE sysubrc.

    lt_params = it_params.

    ls_varid-report    = iv_program.
    ls_varid-variant   = iv_variant.
    ls_varid-environmnt = 'A'.
    ls_varid-mandt     = sy-mandt.
    APPEND VALUE #( mandt = sy-mandt langu = sy-langu report = iv_program
                    variant = iv_variant vtext = iv_text ) TO lt_text.

    CALL FUNCTION 'RS_VARIANT_EXISTS'
      EXPORTING report  = iv_program
                variant = iv_variant
      IMPORTING r_c     = lv_rc
      EXCEPTIONS OTHERS = 1.

    IF sy-subrc = 0 AND lv_rc = 0.
      CALL FUNCTION 'RS_CHANGE_CREATED_VARIANT'
        EXPORTING  curr_report   = iv_program
                   curr_variant  = iv_variant
                   vari_desc     = ls_varid
        TABLES     vari_contents = lt_params
                   vari_text     = lt_text
        EXCEPTIONS OTHERS        = 1.
    ELSE.
      CALL FUNCTION 'RS_CREATE_VARIANT'
        EXPORTING  curr_report   = iv_program
                   curr_variant  = iv_variant
                   vari_desc     = ls_varid
        TABLES     vari_contents = lt_params
                   vari_text     = lt_text
        EXCEPTIONS OTHERS        = 1.
    ENDIF.
    rv_ok = xsdbool( sy-subrc = 0 ).
  ENDMETHOD.

ENDCLASS.
