import logging
import requests
import tarfile
import yaml
import xml.etree.ElementTree as ET
from io import BytesIO
from squad.plugins import Plugin as BasePlugin


logger = logging.getLogger()


class ExtractedResult(object):
    contents = None
    length = None


class ResultFiles(object):
    test_results = None
    tradefed_logcat = None
    tradefed_stdout = None


class Tradefed(BasePlugin):
    name = "Tradefed"
    tradefed_results_url = None

    def _assign_test_log(self, buf, test_list):
        if buf is None:
            logger.warning("Results file doesn't exist")
            return
        # assume buf is a file-like object
        tradefed_tree = ET.parse(buf)
        buf.seek(0)
        for test in test_list:
            # search in etree for relevant test
            logger.debug("processing %s/%s" % (test.suite, test.name))
            test_suite_name_list = str(test.suite).split("/")
            test_suite_name = test_suite_name_list[-1]
            test_suite_abi = None
            if "." in test_suite_name:
                test_suite_abi, test_suite_name = test_suite_name.split(".")
            test_name_list = test.name.rsplit(".")
            test_name = test_name_list[-1]
            logger.debug("searching for %s log" % test_name)
            suite_node = None
            if test_suite_abi is not None:
                # Module name="VtsKernelLtp" abi="armeabi-v7a"
                suite_node = tradefed_tree.find('.//Module[@name="%s"][@abi="%s"]' % (test_suite_name, test_suite_abi))
            else:
                suite_node = tradefed_tree.find('.//Module[@name="%s"]' % (test_suite_name))
            if not suite_node:
                logger.debug("Module %s is not present in the log" % test_suite_name)
                continue
            log_node = suite_node.find('.//Test[@name="%s"]' % test_name)
            if log_node is None:
                test_name = test_name_list[-2] + "." + test_name
                logger.debug("searching for %s log" % test_name)
                log_node = tradefed_tree.find('.//Test[@name="%s"]' % test_name)

            if log_node is not None:
                trace_node = log_node.find('.//StackTrace')
                if trace_node is not None:
                    test.log = trace_node.text
                    test.save()


    def _extract_member(self, tar_file, tar_member):
        extracted_container = ExtractedResult()
        extracted_container.contents = tar_file.extractfile(tar_member)
        extracted_container.length = tar_member.size
        return extracted_container

    def _download_results(self, result_dict):
        results = ResultFiles()
        if 'metadata' in result_dict:
            if 'reference' in result_dict['metadata']:
                try:
                    logger.debug("Downloading CTS/VTS log from: %s" % result_dict['metadata']['reference'])
                    self.tradefed_results_url = result_dict['metadata']['reference']
                    result_tarball_request = requests.get(self.tradefed_results_url)
                    if result_tarball_request.status_code == 200:
                        result_tarball_request.raw.decode_content = True
                        r = BytesIO(result_tarball_request.content)
                        logger.debug("Retrieved %s bytes" % r.getbuffer().nbytes)
                        t = tarfile.open(fileobj=r, mode='r:xz')
                        for member in t.getmembers():
                            logger.debug("Available member: %s" % member.name)
                            if "test_result.xml" in member.name:
                                results.test_results = self._extract_member(t, member)
                                logger.debug("test_results object is empty: %s" % (results.test_results is None))
                            if "tradefed-stdout.txt" in member.name:
                                results.tradefed_stdout = self._extract_member(t, member)
                                logger.debug("tradefed_stdout object is empty: %s" % (results.tradefed_stdout is None))
                            if "tradefed-logcat.txt" in member.name:
                                results.tradefed_logcat = self._extract_member(t, member)
                                logger.debug("tradefed_logcat object is empty: %s" % (results.tradefed_logcat is None))
                except tarfile.ReadError as e:
                    logger.warn(e)
                    logger.warn("tarfile.ReadError")
                except requests.exceptions.Timeout:
                    logger.warn("requests.exceptions.Timeout")
        return results

    def _get_from_artifactorial(self, testjob, suite_name):
        logger.debug("Retrieving result summary for job: %s" % testjob.job_id)
        suites = testjob.backend.get_implementation().proxy.results.get_testjob_suites_list_yaml(testjob.job_id)
        y = None
        try:
            y = yaml.load(suites)
        except yaml.parser.ParserError:
            return None

        if not y:
            logger.debug("Something went wrong when calling results.get_testjob_suites_list_yaml from LAVA")
            return None

        for suite in y:
            if suite_name in suite['name']:
                limit = 500
                offset = 0
                results = testjob.backend.get_implementation().proxy.results.get_testsuite_results_yaml(
                    testjob.job_id,
                    suite['name'],
                    limit,
                    offset)
                yaml_results = None
                try:
                    yaml_results = yaml.load(results, Loader=yaml.CLoader)
                except yaml.scanner.ScannerError:
                    return None

                if not yaml_results:
                    logger.debug("Something went wrong with results.get_testsuite_results_yaml from LAVA")
                    return None

                while True:
                    if len(yaml_results) > 0:
                        for result in yaml_results:
                            if result['name'] == 'test-attachment':
                                return self._download_results(result)
                        offset = offset + limit
                        logger.debug("requesting results for %s with offset of %s"
                                     % (suite['name'], offset))
                        results = testjob.backend.get_implementation().proxy.results.get_testsuite_results_yaml(
                            testjob.job_id,
                            suite['name'],
                            limit,
                            offset)
                        yaml_results = yaml.load(results, Loader=yaml.CLoader)
                    else:
                        break
        return None

    def _create_testrun_attachment(self, testrun, name, extracted_file):
        testrun.attachments.create(
            filename = name,
            data = extracted_file.contents.read(),
            length = extracted_file.length
        )

    def postprocess_testjob(self, testjob):
        # get related testjob
        logger.info("Starting CTS/VTS plugin for test job: %s" % testjob.pk)
        logging.debug("Processing test job: %s" % testjob.job_id)
        if not testjob.backend.implementation_type == 'lava':
            logger.warning("Test job %s doesn't come from LAVA" % testjob.job_id)
            logger.debug(testjob.backend.implementation_type)
            return # this plugin only applies to LAVA
        # check if testjob is a tradefed job
        if testjob.definition:
            logger.debug("Loading test job definition")
            job_definition = yaml.load(testjob.definition)
            # find all tests
            if 'actions' in job_definition.keys():
                for test_action in [action for action in job_definition['actions'] if'test' in action.keys()]:
                    for test_definition in test_action['test']['definitions']:
                        logger.debug("Processing test %s" % test_definition['name'])
                        if "tradefed.yaml" in test_definition['path']:  # is there any better heuristic?
                            # download and parse results
                            results = self._get_from_artifactorial(testjob, test_definition['name'])
                            if results is not None:
                                # add metadata key for taball download
                                testjob.testrun.metadata["tradefed_results_url_%s" % testjob.job_id] = self.tradefed_results_url
                                testjob.testrun.save()
                                # only failed tests have logs
                                if testjob.testrun is not None:
                                    failed = testjob.testrun.tests.filter(result=False)
                                    self._assign_test_log(results.test_results.contents, failed)
                                    if results.test_results is not None:
                                        self._create_testrun_attachment(testjob.testrun, "test_results.xml", results.test_results)
                                    if results.tradefed_stdout is not None:
                                        self._create_testrun_attachment(testjob.testrun, "teadefed_stdout.txt", results.tradefed_stdout)
                                    if results.tradefed_logcat is not None:
                                        self._create_testrun_attachment(testjob.testrun, "teadefed_logcat.txt", results.tradefed_logcat)
        logger.info("Finishing CTS/VTS plugin for test run: %s" % testjob.pk)


