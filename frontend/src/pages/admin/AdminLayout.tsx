import { NavLink, Outlet } from 'react-router-dom'

/** Sub-navigation for the management panel. */
export default function AdminLayout() {
  const link = ({ isActive }: { isActive: boolean }) => (isActive ? 'active' : '')
  return (
    <>
      <div className="subnav">
        <NavLink to="/admin" end className={link}>Fleet</NavLink>
        <NavLink to="/admin/customers" className={link}>Customers</NavLink>
        <NavLink to="/admin/ipam" className={link}>IP space</NavLink>
        <NavLink to="/admin/jobs" className={link}>Jobs</NavLink>
        <NavLink to="/admin/audit" className={link}>Audit</NavLink>
      </div>
      <Outlet />
    </>
  )
}
