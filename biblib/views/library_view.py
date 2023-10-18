"""
Library view
"""
from biblib.views import USER_ID_KEYWORD
from biblib.utils import err, check_boolean
from biblib.models import User, Library, Permissions
from biblib.client import client
from biblib.views.base_view import BaseView
from datetime import datetime
from flask import request, current_app
from flask_discoverer import advertise
from sqlalchemy import Boolean
from sqlalchemy.orm.attributes import flag_modified
from biblib.views.http_errors import MISSING_USERNAME_ERROR, SOLR_RESPONSE_MISMATCH_ERROR, \
    MISSING_LIBRARY_ERROR, NO_PERMISSION_ERROR, BAD_LIBRARY_ID_ERROR


class LibraryView(BaseView):
    """
    End point to interact with a specific library, only returns library content
    if the user has the correct privileges.

    The GET requests are separate from the POST, DELETE requests as this class
    must be scopeless, whereas the others will have scope.
    """
    decorators = [advertise('scopes', 'rate_limit')]
    scopes = []
    rate_limit = [1000, 60*60*24]

    @classmethod
    def get_documents_from_library(cls, library_id, service_uid):
        """
        Retrieve all the documents that are within the library specified
        :param library_id: the unique ID of the library
        :param service_uid: the user ID within this microservice

        :return: bibcodes
        """

        with current_app.session_scope() as session:
            # Get the library
            library = session.query(Library).filter_by(id=library_id).one()

            # Get the owner of the library
            result = session.query(Permissions, User)\
                .join(Permissions.user)\
                .filter(Permissions.library_id == library_id) \
                .filter(Permissions.permissions['owner'].astext.cast(Boolean).is_(True))\
                .one()
            owner_permissions, owner = result

            service = '{api}/{uid}'.format(
                api=current_app.config['BIBLIB_USER_EMAIL_ADSWS_API_URL'],
                uid=owner.absolute_uid
            )
            current_app.logger.info('Obtaining email of user: {0} [API UID]'
                                    .format(owner.absolute_uid))

            response = client().get(
                service
            )

            # For this library get all the people who have permissions
            users = session.query(Permissions).filter_by(
                library_id = library.id
            ).all()

            if response.status_code != 200:
                current_app.logger.error('Could not find user in the API'
                                         'database: {0}.'.format(service))
                owner = 'Not available'
            else:
                owner = response.json()['email'].split('@')[0]

            # User requesting to see the content
            if service_uid:
                try:
                    permission = session.query(Permissions).filter(
                        Permissions.user_id == service_uid
                    ).filter(
                        Permissions.library_id == library_id
                    ).one()

                    if permission.permissions['owner']:
                        main_permission = 'owner'
                    elif permission.permissions['admin']:
                        main_permission = 'admin'
                    elif permission.permissions['write']:
                        main_permission = 'write'
                    elif permission.permissions['read']:
                        main_permission = 'read'
                    else:
                        main_permission = 'none'
                except:
                    main_permission = 'none'
            else:
                main_permission = 'none'

            if main_permission == 'owner' or main_permission == 'admin':
                num_users = len(users)
            elif library.public:
                num_users = len(users)
            else:
                num_users = 0

            metadata = dict(
                name=library.name,
                id='{0}'.format(cls.helper_uuid_to_slug(library.id)),
                description=library.description,
                num_documents=len(library.bibcode),
                date_created=library.date_created.isoformat(),
                date_last_modified=library.date_last_modified.isoformat(),
                permission=main_permission,
                public=library.public,
                num_users=num_users,
                owner=owner
            )
            session.refresh(library)
            session.expunge(library)

            return library, metadata

    @classmethod
    def read_access(cls, service_uid, library_id):
        """
        Defines which type of user has read permissions to a library.

        :param service_uid: the user ID within this microservice
        :param library_id: the unique ID of the library

        :return: boolean, access (True), no access (False)
        """

        read_allowed = ['read', 'write', 'admin', 'owner']
        for access_type in read_allowed:
            if cls.helper_access_allowed(service_uid=service_uid,
                                         library_id=library_id,
                                         access_type=access_type):
                return True

        return False

    @staticmethod
    def timestamp_sort(solr, library_id, reverse=False):
        """
        Take a solr response and sort it based on the timestamps contained in the library
        :input: response: response from SOLR bigquery
        :input: library: The original library
        :input: reverse: returns library by `time desc` if true, `time asc` otherwise.
        
        :return: response: SOLR response sorted by when each item was added.
        """
        if "error" not in solr['response'].keys():
            try:
                 with current_app.session_scope() as session:
                    # Find the specified library
                    library = session.query(Library).filter_by(id=library_id).one()
                    #First we generate a list of timestamps for the valid bibcodes
                    timestamp = [library.bibcode[doc['bibcode']]['timestamp'] for doc in solr['response']['docs']]
                    #Then we sort the SOLR response by the generated timestamp list
                    solr['response']['docs'] = [\
                            doc for (doc, timestamp) in sorted(zip(solr['response']['docs'], timestamp), reverse=reverse, key = lambda stamped: stamped[1])\
                        ]
            except Exception as e:
                current_app.logger.warn("Failed to retrieve timestamps for {} with exception: {}. Returning default sorting.".format(library.id, e))
        else:
            current_app.logger.warn("SOLR bigquery returned status code {}. Stopping.".format(solr['response'].status_code))

        return solr
        
    @staticmethod
    def solr_update_library(library_id, solr_docs):
        """
        Updates the library based on the solr canonical bibcodes response
        :param library: library_id of the library to update
        :param solr_docs: solr docs from the bigquery response

        :return: dictionary with details of files modified
                 num_updated: number of documents modified
                 duplicates_removed: number of files removed for duplication
                 update_list: list of changed bibcodes {'before': 'after'}
        """

        # Definitions
        update = False
        canonical_bibcodes = []
        alternate_bibcodes = {}
        new_bibcode = {}

        # Constants for the return dictionary
        num_updated = 0
        duplicates_removed = 0
        update_list = []

        # Extract the canonical bibcodes and create a hashmap for the
        # alternate bibcodes
        for doc in solr_docs:
            canonical_bibcodes.append(doc['bibcode'])
            if doc.get('alternate_bibcode'):
                alternate_bibcodes.update(
                    {i: doc['bibcode'] for i in doc['alternate_bibcode']}
                )

        with current_app.session_scope() as session:
            library = session.query(Library).filter(Library.id == library_id).one()
            default_timestamp = datetime.timestamp(library.date_created)

            for bibcode in library.bibcode:
                if "timestamp" not in library.bibcode[bibcode].keys():
                    update = True
                    library.bibcode[bibcode]["timestamp"] = default_timestamp
                
                # Skip if its already canonical
                if bibcode in canonical_bibcodes:
                    new_bibcode[bibcode] = library.bibcode[bibcode]
                    continue

                # Update if its an alternate
                if bibcode in alternate_bibcodes.keys():
                    update = True
                    num_updated += 1
                    update_list.append({bibcode: alternate_bibcodes[bibcode]})

                    # Only add the bibcode if it is not there
                    if alternate_bibcodes[bibcode] not in new_bibcode:
                        new_bibcode[alternate_bibcodes[bibcode]] = \
                            library.bibcode[bibcode]
                    else:
                        duplicates_removed += 1

                elif bibcode not in canonical_bibcodes and\
                        bibcode not in alternate_bibcodes.keys():
                    new_bibcode[bibcode] = library.bibcode[bibcode]

            if update:
                # Update the database
                library.bibcode = new_bibcode
                session.add(library)
                flag_modified(library, "bibcode")
                session.commit()

            updates = dict(
                num_updated=num_updated,
                duplicates_removed=duplicates_removed,
                update_list=update_list
            )

            return updates

    # Methods
    def get(self, library):
        """
        HTTP GET request that returns all the documents inside a given
        user's library
        :param library: library ID

        :return: list of the users libraries with the relevant information

        Header:
        -------
        Must contain the API forwarded user ID of the user accessing the end
        point

        Post body:
        ----------
        No post content accepted.

        Return data:
        -----------
        documents:    <list>   Currently, a list containing the bibcodes.
        solr:         <dict>   The response from the solr bigquery end point
        metadata:     <dict>   contains the following:

          name:                 <string>  Name of the library
          id:                   <string>  ID of the library
          description:          <string>  Description of the library
          num_documents:        <int>     Number of documents in the library
          date_created:         <string>  ISO date library was created
          date_last_modified:   <string>  ISO date library was last modified
          permission:           <sting>   Permission type, can be: 'read',
                                          'write', 'admin', or 'owner'
          public:               <boolean> True means it is public
          num_users:            <int>     Number of users with permissions to
                                          this library
          owner:                <string>  Identifier of the user who created
                                          the library

        updates:      <dict>   contains the following

          num_updated:          <int>     Number of documents modified based on
                                          the response from solr
          duplicates_removed:   <int>     Number of files removed because
                                          they are duplications
          update_list:          <list>[<dict>]
                                          List of dictionaries such that a
                                          single element described the original
                                          bibcode (key) and the updated bibcode
                                          now being stored (item)

        Permissions:
        -----------
        The following type of user can read a library:
          - owner
          - admin
          - write
          - read

        Default Pagination Values:
        -----------
        - start: 0
        - rows: 20 (max 100)
        - sort: 'date desc'
        - fl: 'bibcode'

        Additional Pagination options:
        ------------
        - sort:
            - "time asc" sort by time added to library with documents added least recently added documents being listed first.
            - "time desc" sort by time added to library with the most recently added documents being listed first.

        """
        try:
            user = int(request.headers[USER_ID_KEYWORD])
        except KeyError:
            current_app.logger.error('No username passed')
            return err(MISSING_USERNAME_ERROR)

        # Parameters to be forwarded to Solr: pagination, and fields
        try:
            start = int(request.args.get('start', 0))
            max_rows = current_app.config.get('BIBLIB_MAX_ROWS', 100)
            max_rows *= float(
                request.headers.get('X-Adsws-Ratelimit-Level', 1.0)
            )
            max_rows = int(max_rows)
            rows = min(int(request.args.get('rows', 20)), max_rows)
            raw_library = check_boolean(request.args.get('raw', 'false'))

        except ValueError:
            current_app.logger.debug("Raised value error")
            start = 0
            rows = 20
            raw_library = False

        sort = request.args.get('sort', 'date desc')
        #timestamp sorting is handled in biblib so we need to change the sort to something SOLR understands.
        if sort in ['time asc', 'time desc']:
            current_app.logger.debug("sort order is set to{}".format(sort))
            if sort == 'time desc':
                add_sort = True
            else:
                add_sort = False
            sort = 'date desc'

        else: add_sort = None
        
        fl = request.args.get('fl', 'bibcode')
        current_app.logger.info('User gave pagination parameters:'
                                'start: {}, '
                                'rows: {}, '
                                'sort: "{}", '
                                'fl: "{}", '
                                'raw: "{}"'.format(start, rows, sort, fl, raw_library))

        try:
            library = self.helper_slug_to_uuid(library)
        except TypeError:
            return err(BAD_LIBRARY_ID_ERROR)

        current_app.logger.info('User: {0} requested library: {1}'
                                .format(user, library))

        user_exists = self.helper_user_exists(absolute_uid=user)
        if user_exists:
            service_uid = \
                self.helper_absolute_uid_to_service_uid(absolute_uid=user)
        else:
            service_uid = None

        # If the library is public, allow access
        try:
            # Try to load the dictionary and obtain the solr content
            library, metadata = self.get_documents_from_library(
                library_id=library,
                service_uid=service_uid
            )
            # pay attention to any functions that try to mutate the list
            # this will alter expected returns later
            if not raw_library:
                try:
                    solr = self.solr_big_query(
                        bibcodes=library.bibcode,
                        start=start,
                        rows=rows,
                        sort=sort,
                        fl=fl
                    ).json()
                except Exception as error:
                    current_app.logger.warning('Could not parse solr data: {0}'
                                            .format(error))
                    solr = {'error': 'Could not parse solr data'}

                # Now check if we can update the library database based on the
                # returned canonical bibcodes
                if solr.get('response'):
                    # Update bibcodes based on solrs response
                    updates = self.solr_update_library(
                        library_id=library.id,
                        solr_docs=solr['response']['docs']
                    )
                    if add_sort:
                        solr = self.timestamp_sort(solr, library.id, reverse=add_sort)

                    documents = [i['bibcode'] for i in solr['response']['docs']]
                else:
                    # Some problem occurred, we will just ignore it, but will
                    # definitely log it.
                    solr = SOLR_RESPONSE_MISMATCH_ERROR['body']
                    current_app.logger.warning('Problem with solr response: {0}'
                                            .format(solr))
                    updates = {}
                    if add_sort != None:
                        with current_app.session_scope() as session:
                            # Find the specified library (we have to do this to have full access to the library)
                            temp_library = session.query(Library).filter_by(id=library.id).one()
                            sortable_list = [(bibcode, library.bibcode[bibcode]["timestamp"]) for bibcode in temp_library.get_bibcodes()]
                            sortable_list.sort(key = lambda stamped: stamped[1], reverse=add_sort)
                            documents = [doc[0] for doc in sortable_list]         
                    else:
                        documents = library.get_bibcodes()
                        documents.sort()
                    documents = documents[start:start+rows]
            
            else:
                solr = 'Only the raw library was requested.'
                current_app.logger.info('User: {0} requested only raw library output'
                                            .format(user))
                updates = {}
                documents = library.get_bibcodes()
                documents.sort()
                documents = documents[start:start+rows]

            # Make the response dictionary
            response = dict(
                documents=documents,
                solr=solr,
                metadata=metadata,
                updates=updates
            )

        except Exception as error:
            current_app.logger.warning(
                'Library missing or solr endpoint failed: {0}'
                .format(error)
            )
            return err(MISSING_LIBRARY_ERROR)

        # Skip anymore logic if the library is public or the exception token is present
        special_token = current_app.config.get('READONLY_ALL_LIBRARIES_TOKEN')
        if library.public or (special_token and request.headers.get('Authorization', '').endswith(special_token)):
            current_app.logger.info('Library: {0} is public'
                                    .format(library.id))
            return response, 200
        else:
            current_app.logger.warning('Library: {0} is private'
                                       .format(library.id))

        # If the user does not exist then there are no associated permissions
        # If the user exists, they will have permissions
        if self.helper_user_exists(absolute_uid=user):
            service_uid = \
                self.helper_absolute_uid_to_service_uid(absolute_uid=user)
        else:
            current_app.logger.error('User:{0} does not exist in the database.'
                                     ' Therefore will not have extra '
                                     'privileges to view the library: {1}'
                                     .format(user, library.id))

            return err(NO_PERMISSION_ERROR)

        # If they do not have access, exit
        if not self.read_access(service_uid=service_uid,
                                library_id=library.id):
            current_app.logger.error(
                'User: {0} does not have access to library: {1}. DENIED'
                .format(service_uid, library.id)
            )
            return err(NO_PERMISSION_ERROR)

        # If they have access, let them obtain the requested content
        current_app.logger.info('User: {0} has access to library: {1}. '
                                'ALLOWED'
                                .format(user, library.id))

        return response, 200
